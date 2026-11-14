#!/usr/bin/env python3
"""Role=E7 response probe — A/B test for the role-reversal hypothesis.

BACKGROUND
  The 913 MCU, with nothing answered, natively loops a two-frame handshake
  prefix and retries it every ~2.6s (SEQ resets to 01 each retry):
      01 AA 18 00   (hello)
      02 AA 09 32   (cmd09 data32)
  Our harness answers with role=AA frames, which the MCU ignores — AA appears
  to be the MCU's own transmit role. So the 913 never sees a valid module and
  just re-broadcasts the prefix.

  Hypothesis (roles reversed from the harness/primer assumption):
      0xE7 = module -> MCU      0xAA = MCU -> module
  If true, answering the MCU's prefix with role=E7 requests should make it
  progress (send an ID blob / config / stop retrying).

WHAT THIS PROBE DOES
  1. waits for the MCU's hello + cmd09 prefix,
  2. answers with requests in a configurable ROLE byte (--role, default E7;
     default: just readID cmd02; --full sends the whole module request
     sequence from the capture),
  3. watches the MCU's reaction and reports whether it progressed past the
     hello/cmd09 retry loop.

  Run with --role E7 and then --role AA (and 55) to A/B the role-direction
  question: whichever role makes the 913 progress past its retry loop is the
  role it expects from the module. (Only meaningful once the 913 RX is
  ungated — see the module-present / enable pin.)

USAGE  (run from source/, same dir as kwikset_bench.py)
  ../.venv/bin/python probe_e7.py -p /dev/tty.usbserial-XXXX -v
  ../.venv/bin/python probe_e7.py -p /dev/tty.usbserial-XXXX -v --full
  ../.venv/bin/python probe_e7.py -p /dev/tty.usbserial-XXXX -v --after hello
  ../.venv/bin/python probe_e7.py -p /dev/tty.usbserial-XXXX -v --role AA
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kwikset_bench as kb

# The MCU's native two-frame handshake prefix (what we are answering).
HELLO_CMD = 0x18
PREFIX_CMD = 0x09          # the second frame of the prefix (cmd09 data32)

# Module request frames (role=E7) to send in response, in order. From the
# capture, the module's (E7) request sequence was:
#   readID x3, cmd07, cmd08, cmd09, cmd35 (config), cmd30, cmd25 poll.
FULL_REQS = [
    (0x02, []),               # readID
    (0x07, []),               # cmd07
    (0x08, []),               # cmd08
    (0x09, []),               # cmd09
    (0x35, []),               # cmd35 (config)
    (0x30, []),               # cmd30
    (0x25, [0x27, 0x00]),     # cmd25 status poll (data 27 00 as captured)
]
MIN_REQS = [(0x02, [])]       # minimal A/B: just readID

# (role,cmd) of the MCU's native retry loop — anything else counts as progress.
RETRY_LOOP = {(0xAA, 0x18), (0xAA, 0x09)}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", required=True)
    ap.add_argument("--swap", action="store_true", help="swap TX/RX on the adapter")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--full", action="store_true",
                    help="send the full module request sequence, not just readID")
    ap.add_argument("--role", default="E7",
                    help="ROLE byte of the frames WE send (default E7; pass AA or 55 "
                         "to A/B the role-direction question once the 913 RX is ungated)")
    ap.add_argument("--after", choices=["cmd09", "hello"], default="cmd09",
                    help="fire the answer after the MCU's %s "
                         "(cmd09 = end of prefix [default]; hello = after frame one)")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="how long to observe (default 30)")
    ap.add_argument("--gap", type=float, default=0.3,
                    help="seconds between request frames when --full (default 0.3)")
    a = ap.parse_args()

    kb.VERBOSE = a.verbose
    reqs = FULL_REQS if a.full else MIN_REQS
    try:
        send_role = int(a.role, 16)
    except ValueError:
        ap.error("--role must be hex, e.g. E7 / AA / 55")
    rname = a.role.upper()

    s = kb.open_port(a.port, swap=a.swap)
    print("Response-role probe (A/B for the role-direction question).")
    print("  answer = role=%s [" % rname + ", ".join("cmd%02X" % c for c, _ in reqs) + "]")
    print("  fire after MCU's: " + a.after)
    print("  watching for the 913 to STOP its hello/cmd09 retry loop.\n")

    end = time.time() + a.seconds
    saw = {"hello": False, "cmd09": False}
    answered = False
    seq = 0x01
    seen = {}          # (role,cmd) -> count of MCU frames
    progress_frames = []

    def fire():
        nonlocal answered, seq
        print("\n>>> MCU prefix complete -> answering with role=%s:" % rname)
        for rcmd, rd in reqs:
            kb.tx(s, kb.build_frame(seq, send_role, rcmd, rd),
                  "%s cmd%02X %s seq=%02X" % (rname, rcmd,
                                              " ".join("%02X" % x for x in rd) or "-",
                                              seq))
            seq = (seq + 1) & 0xFF
            if a.gap:
                time.sleep(a.gap)
        answered = True

    try:
        while time.time() < end:
            b = kb.recv_frame(s, timeout=1.5)
            if not b:
                continue
            f = kb.log_frame("<-", b)
            if not f:
                continue
            role, cmd = f["fields"][2], f["fields"][3]
            seen[(role, cmd)] = seen.get((role, cmd), 0) + 1
            if cmd == HELLO_CMD and role == 0xAA:
                saw["hello"] = True
            if cmd == PREFIX_CMD and role == 0xAA:
                saw["cmd09"] = True

            if not answered:
                if a.after == "hello" and saw["hello"]:
                    fire()
                elif a.after == "cmd09" and saw["hello"] and saw["cmd09"]:
                    fire()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        s.close()

    print("\n=== MCU frames observed (role,cmd : count) ===")
    for (role, cmd), n in sorted(seen.items()):
        tag = "  <- retry-loop" if (role, cmd) in RETRY_LOOP else "  *** NEW"
        print("  role=%02X cmd=%02X  x%-3d%s" % (role, cmd, n, tag))
    new = [k for k in seen if k not in RETRY_LOOP]
    if new:
        print("\n>>> PROGRESS: the 913 sent frame(s) beyond its hello/cmd09 retry loop:")
        for (role, cmd) in new:
            print("      role=%02X cmd=%02X" % (role, cmd))
        print("    -> supports the role-reversal hypothesis (E7 = module->MCU).")
    else:
        print("\n>>> NO PROGRESS: the 913 only repeated its hello/cmd09 retry loop.")
        print("    -> the role=E7 answer was ignored. Either the roles are NOT reversed,")
        print("       or the module's first reply is a different frame than readID.")
        print("    Try: --full  (whole request sequence)   and/or   --after hello")


if __name__ == "__main__":
    main()