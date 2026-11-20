"""Handshake + validation (corrected direction).

The MCU initiates; we answer with E7 requests; it replies (AA/55) echoing our
SEQ. These helpers implement exactly that flow. (They replace the old
backwards RESPS/make_reply model, where WE answered the MCU's requests.)
"""
import time

from .frames import build_frame
from .link import log_frame, recv_frame, tx
from .protocol import (EXPECTED, REQS, ROLE_MCU_AX, ROLE_MOD,
                       fmt_data, state_word)

def send_reqs(s, seq):
    """Transmit our module request sequence (all role=E7). Returns next free SEQ."""
    for cmd, data in REQS:
        tx(s, build_frame(seq, ROLE_MOD, cmd, data),
           "req cmd%02X %s seq=%02X" % (cmd, fmt_data(data), seq))
        seq = (seq + 1) & 0xFF
    return seq

def check_expected(f):
    """Validate an incoming 913 frame against this board's expected reply.
    Silent on a match; one warning line if role or data differ from EXPECTED.
    cmd07 is special: its reply data byte is the lock-state readout (01 locked
    / 02 unlocked on this bench board; 00 on the capture board) -- logged as
    info, never as a mismatch."""
    cmd, role, data = f["fields"][3], f["fields"][2], f["fields"][4:]
    if cmd == 0x07:
        state = state_word(data[0] if data else None)
        print("      ^ cmd07 reply data=%s -> lock state: %s" % (fmt_data(data), state))
        return
    exp = EXPECTED.get(cmd)
    if exp is None:
        return
    e_role, e_data = exp
    if role != e_role or data != e_data:
        print("      ^^ MISMATCH cmd%02X: want role=%02X data=%s, got role=%02X data=%s"
              % (cmd, e_role, fmt_data(e_data), role, fmt_data(data)))

def poke_round(s, seq, label="POKE"):
    """Send one poke round: our full E7 request sequence (REQS) plus a cmd07
    state poll and a cmd12 settings GET. Returns the next free SEQ. Shared by
    the standalone `poke` subcommand and the --with-poke prefix wait."""
    for cmd, data in REQS:
        tx(s, build_frame(seq, ROLE_MOD, cmd, data),
           "%s cmd%02X %s seq=%02X" % (label, cmd, fmt_data(data), seq))
        seq = (seq + 1) & 0xFF
    tx(s, build_frame(seq, ROLE_MOD, 0x07, []),
       "%s cmd07 state poll seq=%02X" % (label, seq))
    seq = (seq + 1) & 0xFF
    tx(s, build_frame(seq, ROLE_MOD, 0x12, []),
       "%s cmd12 settings seq=%02X" % (label, seq))
    return (seq + 1) & 0xFF

def wait_prefix(s, timeout=15.0, after="cmd09", poke_interval=0.0):
    """Wait for the 913's boot prefix. The MCU INITIATES: it transmits the hello
    (AA cmd18), then a cmd09 retry frame, and re-sends the pair every ~2.6 s
    (SEQ resets to 01) until it gets a valid module answer. Every frame is
    logged (verbose-aware) as it arrives. Returns (how, seq):
      'prefix'  the requested sync point (`after`: 'hello' or 'cmd09') was seen.
      'poke'    --with-poke is on and the MCU answered a poke's cmd07 state
                poll (a session was established without a prefix -- the poke
                round already carried our full E7 request sequence).
      None      timeout; the replies seen while waiting are tallied for the
                deaf-state diagnostic (PROTOCOL.md 14.12: a post-autolock deaf
                MCU answers cmd08/cmd09 only).
    seq is the next free SEQ after the pokes sent (0x01 if none)."""
    saw = {"hello": False, "cmd09": False}
    replies = {}
    seq = 0x01
    start = time.time()
    last_poke = start
    while time.time() - start < timeout:
        if poke_interval and time.time() - last_poke >= poke_interval:
            seq = poke_round(s, seq, "POKE (prefix wait)")
            last_poke = time.time()
        b = recv_frame(s, timeout=1.0)
        if not b:
            continue
        f = log_frame("<-", b)
        if not f:
            continue
        role, cmd = f["fields"][2], f["fields"][3]
        data = f["fields"][4:]
        if role == ROLE_MCU_AX:
            replies[cmd] = replies.get(cmd, 0) + 1
            if poke_interval and cmd == 0x07 and data:
                return ("poke", seq)
        if role == ROLE_MCU_AX and cmd == 0x18:
            saw["hello"] = True
        if role == ROLE_MCU_AX and cmd == 0x09:
            saw["cmd09"] = True
        if (after == "hello" and saw["hello"]) or \
           (after == "cmd09" and saw["hello"] and saw["cmd09"]):
            return ("prefix", seq)
    if replies:
        got = ", ".join("cmd%02X x%d" % (c, n) for c, n in sorted(replies.items()))
        print("  no prefix, but the MCU answered while waiting: %s" % got)
        if set(replies) <= {0x08, 0x09}:
            print("  -> that is the post-autolock 'deaf' state (PROTOCOL.md 14.12):")
            print("     the MCU answers cmd08/cmd09 only. Power-cycle the 913 to recover.")
    return (None, seq)

def do_handshake(s, hello_timeout=15.0, after="cmd09", poke_interval=0.0):
    """Corrected handshake: wait for the MCU's prefix, then transmit our E7
    request sequence. Returns the next free SEQ, or None if no prefix was seen
    (e.g. the enable pin is not driven High). With poke_interval > 0
    (--with-poke), the prefix wait is active: a poke round is sent every
    poke_interval seconds, so a lock that was NOT power-cycled (no prefix
    expected) still gets probed; a re-sent prefix or a cmd07 answer both
    start the session."""
    want = "hello" if after == "hello" else "hello+cmd09"
    print("Waiting for the 913's boot prefix (%s)..." % want)
    if poke_interval:
        print("  --with-poke: poking (E7 sequence + cmd07 + cmd12) every %.0fs while waiting."
              % poke_interval)
    how, pseq = wait_prefix(s, timeout=hello_timeout, after=after,
                            poke_interval=poke_interval)
    if how is None:
        if poke_interval:
            print("No prefix in %.0fs (poked while waiting). Power-cycle the 913 and"
                  % hello_timeout)
            print("  re-run -- or check the adapter/wiring if it never replied at all.")
        else:
            print("No prefix in %.0fs. Is the enable pin tied High and the 913 power-cycled?"
                  % hello_timeout)
            print("  (the boot prefix is sent ONLY at power-on: power-cycle AFTER this script started)")
        return None
    if how == "poke":
        print("Session established via poke (the MCU answered our cmd07 request; no prefix).")
        return pseq
    if pseq != 0x01:
        print("Prefix seen -> transmitting our E7 request sequence (SEQ resumes at %02X after the pokes)."
              % pseq)
        return send_reqs(s, pseq)
    print("Prefix seen -> transmitting our E7 request sequence.")
    return send_reqs(s, 0x01)     # 913 echoes our SEQ in replies (match probe_e7.py)
