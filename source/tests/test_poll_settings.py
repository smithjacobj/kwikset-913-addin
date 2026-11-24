#!/usr/bin/env python3
"""End-to-end test of poll(settings=True) against a fake MCU.

Same fake as test_poll.py (tests.fakes.McuFake) plus a dipswitch image:
starts at 0x05 (LED+Buzzer, the factory default) and flips to 0x04 (Buzzer
only) at t=8 s, i.e. the user toggled the Lock Status LED switch off. The
fake answers cmd12 GETs with the current image (even during the
cmd07-silent window -- the real MCU's silence was about state replies).
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
    poll(McuFake(dip=0x05, dip_flip=0x04), seconds=22, interval=2, keepalive=2, settings=True, hello_timeout=15, after="cmd09")
out = buf.getvalue()
print(out)
checks = {
    # state path (unchanged from test_poll.py)
    "state change flagged": "*** STATE CHANGE: unlocked -> LOCKED ***" in out,
    "state summary": "state changes: 1" in out and "final state: LOCKED" in out,
    # settings path
    "initial settings readout": "settings: 05 = Lock Status LED, Buzzer  (initial readout)" in out,
    "settings change flagged": "*** SETTINGS CHANGE: 05 (Lock Status LED, Buzzer) -> 04 (Buzzer) ***" in out,
    "unchanged settings lines": "settings: 04 = Buzzer  (unchanged)" in out,
    "settings summary": "settings changes: 1" in out and "final settings: 04 = Buzzer" in out,
}
for k, v in checks.items():
    print(("PASS" if v else "FAIL"), "-", k)
assert all(checks.values()), "some checks failed"
print("ALL CHECKS PASSED")
