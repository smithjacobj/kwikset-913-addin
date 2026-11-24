#!/usr/bin/env python3
"""End-to-end test of poll() against a fake MCU (no hardware).

Uses tests.fakes.McuFake in its default scenario: boots with the AA cmd18 +
AA cmd09 prefix (re-sent every ~2.6 s), answers the handshake reqs
bench-style (no cmd09 reply in default mode; cmd30 replies role 55), acks
cmd25. Starts UNLOCKED (state byte 02); at t=8 s flips to LOCKED (01) to
simulate a manual lock; goes silent on cmd07 replies for t in [12,18) to
exercise the no-reply 'unknown' path (keepalive acks keep flowing).
"""
import os
import sys
import io
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import McuFake
from kwikset_bench.modes.poll import poll

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    poll(McuFake(), seconds=22, interval=2, keepalive=2, hello_timeout=15, after="cmd09")
out = buf.getvalue()
print(out)
checks = {
    "handshake state word printed": "lock state: unlocked" in out,
    "initial readout": "unlocked  (initial readout)" in out,
    "state change flagged": "*** STATE CHANGE: unlocked -> LOCKED ***" in out,
    "unchanged lines": "(unchanged)" in out,
    "no-reply -> unknown": "no cmd07 reply -> unknown (no state change)" in out,
    "summary counts": "state changes: 1" in out and "final state: LOCKED" in out,
}
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), "-", k)
assert all(checks.values()), "some checks failed"
print("ALL CHECKS PASSED")
