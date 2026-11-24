"""manual: capture what the MCU transmits when the lock is operated BY HAND.

=====================================================================
MANUAL-OP CAPTURE  (lock operated BY HAND -> what does the MCU transmit?)
=====================================================================
For the Z-Wave add-in board the module must react to manual (key) ops, so
capture what the MCU puts on the UART when the bolt moves without a UART
command. Flow: handshake (like act), then listen for --seconds:
  * Every incoming frame is logged. A frame matching the (seq,cmd) of one of
    OUR requests is a reply to us (silent for cmd25 poll acks); anything else
    is flagged *** MCU-INITIATED *** -- the manual-event candidates.
  * As the user FINISHES a manual op they type a label ('lock'/'unlock') +
    Enter in the console: a MARK line is stamped and we send one cmd07 state
    readout (the state the MCU believes, logged by check_expected). This
    yields a state signal even if the MCU stays silent on manual ops.
  * >10 s between MCU frames prints a quiet marker.
Start this script FIRST, then power-cycle the lock (boot prefix is power-on
only). --poll 0 = fully quiet listen (the session may idle out -- the hello
retry loop would then be visible as MCU-initiated).
"""
import sys
import time

from ..frames import build_frame
from ..handshake import check_expected, do_handshake
from ..link import log_frame, recv_frame, tx
from ..protocol import REQS, ROLE_MCU_AX, ROLE_MOD, STEADY_REQ, fmt_data

def manual(s, seconds=120, hello_timeout=15.0, after="hello", poll=2.0,
           poke_interval=0.0):
    print("Manual-op capture: handshake, then listen %ds while you operate the lock by hand."
          % seconds)
    print("  As you FINISH a manual op, type a label (e.g. 'lock'/'unlock') + Enter:")
    print("  -> a MARK line is stamped and we poll cmd07 once (the state the MCU believes).")
    print("  Frames we did not request are flagged *** MCU-INITIATED *** (event candidates).")
    print("  Note the LED too (ground truth): green blink = unlocked, amber = locked.")
    seq = do_handshake(s, hello_timeout=hello_timeout, after=after,
                       poke_interval=poke_interval)
    if seq is None:
        return
    select = None
    stdin_tty = False
    try:
        import select as _select
        select = _select
        stdin_tty = sys.stdin.isatty()
    except Exception:
        pass
    pending = {}          # our SEQ -> (cmd we sent, why); a match = reply, not event
    for i, (cmd, _data) in enumerate(REQS):
        pending[(0x01 + i) & 0xFF] = (cmd, "handshake")
    t0 = time.time()
    end = t0 + seconds
    last_poll = t0
    last_rx = t0
    marks = []
    counts = {}
    initiated = {}
    def _stamp(label):
        nonlocal seq
        now = time.time()
        marks.append((now, label))
        print("\n=== MARK: %s  (t=%.1fs) ===" % (label, now - t0))
        q = seq & 0xFF
        seq = (q + 1) & 0xFF
        pending[q] = (0x07, "post-mark cmd07")
        tx(s, build_frame(q, ROLE_MOD, 0x07, []),
           "post-mark cmd07 state poll seq=%02X" % q)
    try:
        while time.time() < end:
            if stdin_tty:
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if r:
                    line = sys.stdin.readline()
                    if line == "":            # EOF (Ctrl-D): stop taking marks
                        stdin_tty = False
                    else:
                        _stamp(line.strip() or "mark")
            b = recv_frame(s, timeout=0.2)
            if b:
                gap = time.time() - last_rx
                last_rx = time.time()
                if gap > 10.0:
                    print("  ~~~ quiet for %.1fs ~~~" % gap)
                f = log_frame("<-", b)
                if not f:
                    continue
                role, ccmd, sseq = f["fields"][2], f["fields"][3], f["fields"][1]
                rc = (role, ccmd)
                counts[rc] = counts.get(rc, 0) + 1
                check_expected(f)
                p = pending.get(sseq)
                if p is not None and p[0] == ccmd:
                    pending.pop(sseq, None)
                    if rc != (ROLE_MCU_AX, 0x25):
                        print("      ^ reply to our %s" % p[1])
                else:
                    initiated[rc] = initiated.get(rc, 0) + 1
                    print("  *** MCU-INITIATED: role=%02X cmd=%02X ***" % rc)
            if poll and time.time() - last_poll >= poll:
                q = seq & 0xFF
                seq = (q + 1) & 0xFF
                pending[q] = (0x25, "cmd25 poll")
                tx(s, build_frame(q, ROLE_MOD, STEADY_REQ[0], STEADY_REQ[1]),
                   "status cmd25 %s seq=%02X" % (fmt_data(STEADY_REQ[1]), q))
                last_poll = time.time()
    except KeyboardInterrupt:
        print("\nstopped")
    print("\n=== MANUAL SUMMARY ===")
    print("  incoming (role,cmd):count   [MCU-init = not a reply to us]")
    for rc, n in sorted(counts.items()):
        tag = "   [MCU-init x%d]" % initiated[rc] if rc in initiated else ""
        print("    role=%02X cmd=%02X  x%-3d%s" % (rc[0], rc[1], n, tag))
    for now, label in marks:
        print("  MARK t=%7.1fs  '%s'" % (now - t0, label))
    if not initiated:
        print("  -> No MCU-initiated frames: the MCU only replied to our requests.")
        print("     If the bolt moved by hand, the post-mark cmd07 readouts (and the")
        print("     LED) are the only state signal -- the MCU may be silent on manual ops.")