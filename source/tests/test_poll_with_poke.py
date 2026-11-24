#!/usr/bin/env python3
"""poll() with --with-poke (poke_interval=3.0) against five fake MCUs.

1. FakeDeaf       : no prefix, never answers -> poked prefix wait times out,
                    no diagnostic, poll gives up (no summary).
2. FakeDeafSig    : no prefix, answers ONLY cmd08 00 + cmd09 64 (the 11-20
                    post-autolock deaf signature) -> wait times out WITH the
                    deaf-state diagnostic; poll gives up.
3. McuFake(table=True, lockstate=0x01, prefix_delay=None)
                   : no prefix, fully awake, answers every E7 bench-style ->
                    the poke's cmd07 answer establishes the session WITHOUT
                    a prefix ("poke" path): exactly 8 E7 frames (one poke
                    round), then the poll runs to "final state: LOCKED".
4. McuFake(table=True, lockstate=0x01, prefix_delay=1.2, prefix_repeat=False)
                   : power-cycled before t0, prefix at t=1.2 s (BEFORE the
                    first poke at 3 s) -> plain passive regression: REQS from
                    SEQ 01, no pokes, no "SEQ resumes".
5. McuFake(table=True, lockstate=0x01, prefix_delay=4.2, prefix_repeat=False,
                    awake_from=4.2)
                   : asleep (ignores E7) until t=4.2 s (AFTER the 3 s poke)
                    -> prefix-after-pokes path: "SEQ resumes at 09 after the
                    pokes", then the REQS from SEQ 09.
"""
import os
import sys
import io
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeDeaf, FakeDeafSig, McuFake
from kwikset_bench.modes.poll import poll

REQ_ORDER = [0x02, 0x07, 0x08, 0x09, 0x35, 0x30]

def e7_pairs(fake):
    return [(c, q) for (r, c, q) in fake.tx if r == 0xE7]

def run(fake, after="cmd09", **kw):
    # after is pinned to the full two-frame prefix: with the hello-sync default
    # the boot cmd09 nudge would be left queued and drain mid-handshake (a
    # MISMATCH line), shifting the output these checks calibrate against.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        poll(fake, after=after, **kw)
    return fake, buf.getvalue()

def report(name, out, checks):
    print("\n===== %s =====" % name)
    print(out)
    ok = True
    for k, v in checks.items():
        print(("PASS" if v else "FAIL"), "-", name, "-", k)
        ok = ok and v
    return ok

POKE_SEQ8 = [(REQ_ORDER[i], i + 1) for i in range(6)] + [(0x07, 7), (0x12, 8)]
ALL_OK = True

# 1. silent + poked wait -> timeout, no diagnostic, give up
fake, out = run(FakeDeaf(), seconds=5, interval=2.0, keepalive=0.0,
                 hello_timeout=7, poke_interval=3.0)
ALL_OK &= report("SILENT", out, {
    "poke banner": "--with-poke: poking (E7 sequence + cmd07 + cmd12) every 3s while waiting." in out,
    "2 poke rounds": out.count("POKE (prefix wait) cmd07 state poll") == 2,
    "no deaf diagnostic (nothing answered)": "no prefix, but the MCU answered" not in out,
    "timeout guidance": "No prefix in 7s (poked while waiting)" in out,
    "poll gave up (no summary)": "=== POLL SUMMARY ===" not in out,
})

# 2. deaf signature + poked wait -> timeout WITH deaf diagnostic
fake, out = run(FakeDeafSig(), seconds=5, interval=2.0, keepalive=0.0,
                 hello_timeout=7, poke_interval=3.0)
ALL_OK &= report("DEAFSIG", out, {
    "tallied replies": "no prefix, but the MCU answered while waiting: cmd08 x2, cmd09 x2" in out,
    "deaf diagnostic": "post-autolock 'deaf' state (PROTOCOL.md 14.12)" in out,
    "power-cycle guidance": "Power-cycle the 913 to recover." in out,
    "timeout guidance": "No prefix in 7s (poked while waiting)" in out,
    "poll gave up (no summary)": "=== POLL SUMMARY ===" not in out,
})

# 3. awake, no prefix -> session via poke; exactly 8 E7 frames; poll runs
fake, out = run(McuFake(lockstate=0x01, flip_at=None, silent=None, prefix_delay=None, table=True), seconds=6, interval=2.0, keepalive=0.0,
                 hello_timeout=8, poke_interval=3.0)
e7 = e7_pairs(fake)
ALL_OK &= report("ALIVE", out, {
    "poke banner": "--with-poke: poking (E7 sequence + cmd07 + cmd12) every 3s while waiting." in out,
    "poke session line": "Session established via poke (the MCU answered our cmd07 request; no prefix)." in out,
    "no REQS re-send": "SEQ resumes" not in out,
    "exactly one poke round (8 E7 frames, no repeats)": len([q for c, q in e7 if q <= 8]) == 8,
    "first 8 E7 = REQS + cmd07 + cmd12": e7[:8] == POKE_SEQ8,
    "post-handshake E7 are poll cmd07s only": all(c == 0x07 for c, q in e7[8:]),
    "poll ran": "Draining handshake replies..." in out and "initial readout" in out,
    "final state": "final state: LOCKED" in out,
})

# 4. passive regression: prefix at 1.2s (before the 3s poke) -> REQS from 01
fake, out = run(McuFake(lockstate=0x01, flip_at=None, silent=None, prefix_delay=1.2, prefix_repeat=False, table=True), seconds=6, interval=2.0, keepalive=0.0,
                 hello_timeout=8, poke_interval=3.0)
e7 = e7_pairs(fake)
ALL_OK &= report("BOOT-PASSIVE", out, {
    "plain prefix line": "Prefix seen -> transmitting our E7 request sequence." in out,
    "no seq-resume": "SEQ resumes" not in out,
    "no pokes sent": "POKE (prefix wait)" not in out,
    "REQS from 01": e7[:6] == [(REQ_ORDER[i], i + 1) for i in range(6)],
    "post-handshake E7 are poll cmd07s only": all(c == 0x07 for c, q in e7[6:]),
    "final state": "final state: LOCKED" in out,
})

# 5. prefix AFTER a poke: asleep until power-cycle at 4.2s -> resume at 09
fake, out = run(McuFake(lockstate=0x01, flip_at=None, silent=None, prefix_delay=4.2, prefix_repeat=False, table=True, awake_from=4.2), seconds=6, interval=2.0, keepalive=0.0,
                 hello_timeout=8, poke_interval=3.0)
e7 = e7_pairs(fake)
exp14 = POKE_SEQ8 + [(REQ_ORDER[i], 9 + i) for i in range(6)]
ALL_OK &= report("BOOT-AFTER-POKE", out, {
    "poke sent": out.count("POKE (prefix wait) cmd07 state poll") == 1,
    "seq resumes line": "Prefix seen -> transmitting our E7 request sequence (SEQ resumes at 09 after the pokes)." in out,
    "E7 seqs 1-8 then 9-14": e7[:14] == exp14,
    "post-handshake E7 are poll cmd07s only": all(c == 0x07 for c, q in e7[14:]),
    "final state": "final state: LOCKED" in out,
})

print("\n" + ("ALL CHECKS PASSED" if ALL_OK else "SOME CHECKS FAILED"))
