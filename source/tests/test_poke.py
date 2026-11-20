#!/usr/bin/env python3
"""poke() against three fake MCUs (post-11-20 semantics).
FakeDeaf: no boot prefix, never answers -- poke must run all rounds and
report the fully-silent summary.
FakeAsleep: no boot prefix, answers nothing in round 1 (seq 01-08) but
starts acking every E7 frame from round 2 (seq 09+) -- poke must report
WAKE in round 2 with the full-command-set (WOKEN) summary.
FakeDeafSig: no boot prefix, answers ONLY cmd08 00 + cmd09 64 (the
11-20 post-autolock deaf signature) -- poke must WAKE in round 1 and
print the DEAF STATE summary (power-cycle guidance)."""
import os
import sys
import io
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import FakeAsleep, FakeDeaf, FakeDeafSig
from kwikset_bench.modes.poke import poke

def run(fake):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        poke(fake, rounds=3, interval=0.3, listen=0.5)
    return buf.getvalue()

out_deaf = run(FakeDeaf())
print(out_deaf)
deaf_checks = {
    "no wake reported": "WAKE" not in out_deaf,
    "silent summary": "no reply in any round" in out_deaf,
    "power-cycle guidance": "Power-cycle the 913" in out_deaf,
}
out_asleep = run(FakeAsleep())
print(out_asleep)
asleep_checks = {
    "wake in round 2": "WAKE: first reply in round 2" in out_asleep,
    "woke summary": "WOKEN" in out_asleep and "merely idle, not deaf" in out_asleep,
    "re-handshake hint": "Re-run a normal handshake" in out_asleep,
}
out_deafstate = run(FakeDeafSig())
print(out_deafstate)
deafstate_checks = {
    "wake in round 1": "WAKE: first reply in round 1" in out_deafstate,
    "2 frames": "2 frame(s)" in out_deafstate,
    "deaf-state summary": "DEAF STATE" in out_deafstate,
    "signature listed": "cmd08 x1" in out_deafstate and "cmd09 x1" in out_deafstate,
    "power-cycle guidance": "Power-cycle the 913" in out_deafstate,
    "not the full set": "does NOT restore the full command set" in out_deafstate,
}
for name, checks in (("DEAF", deaf_checks), ("ASLEEP", asleep_checks),
                     ("DEAFSTATE", deafstate_checks)):
    for k, v in checks.items():
        print(("PASS" if v else "FAIL"), "-", name, "-", k)
assert all(deaf_checks.values()) and all(asleep_checks.values()) and all(deafstate_checks.values()), "some checks failed"
print("ALL CHECKS PASSED")
