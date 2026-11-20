#!/usr/bin/env python3
"""read_state() unit test against a fake serial (no hardware).

The fake replies to our E7 cmd07 requests with an AA cmd07 carrying `state`,
echoing our SEQ. `skip_first=N` makes the MCU drop the first N replies (the
known flaky-skip); state=None -> it never replies. Covers the immediate,
flaky-skip, and never-replies paths of the post-command state confirmation.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import Base
from kwikset_bench.frames import build_frame
from kwikset_bench.modes.act import read_state
from kwikset_bench.protocol import state_word


class FakeSerial(Base):
    """Replies to our E7 cmd07 requests with an AA cmd07 carrying `state`,
    echoing our SEQ. `skip_first=N` makes the MCU drop the first N replies
    (the known flaky-skip). state=None -> it never replies."""
    def __init__(self, state, skip_first=0):
        super().__init__()
        self.state = state
        self.skip = skip_first
        self.replies_sent = 0

    def _on_frame(self, f):
        if f["fields"][2] == 0xE7 and f["fields"][3] == 0x07:
            self.replies_sent += 1
            if self.state is not None and self.replies_sent > self.skip:
                self._rx += build_frame(f["fields"][1], 0xAA, 0x07, self.state)


def run(label, state, skip_first):
    s = FakeSerial(state, skip_first)
    t0 = time.time()
    st, seq = read_state(s, 0x10, retries=3, per_retry=0.3)
    dt = time.time() - t0
    exp_word = state_word(st)
    state_s = ("%02X" % state[0]) if state is not None else "None"
    print("%-22s state=%-4s skip=%d -> got=%-4s (%s) next_seq=%02X  in %.2fs"
          % (label, state_s,
             skip_first, ("%02X" % st) if st is not None else "None",
             exp_word, seq, dt))
    return st

a = run("immediate-LOCKED",  [0x01], 0)
b = run("immediate-unlocked", [0x02], 0)
c = run("flaky(skip1)-LOCKED", [0x01], 1)
d = run("never-replies",      None,   99)
assert a == 0x01, a
assert b == 0x02, b
assert c == 0x01, c
assert d is None, d
print("\nALL CONFIRM TESTS PASSED")
print("PASS/FAIL check: lock(expect 01) read", state_word(a),
      "=>", "PASS" if a == 0x01 else "FAIL")
print("PASS/FAIL check: unlock(expect 02) read", state_word(b),
      "=>", "PASS" if b == 0x02 else "FAIL")