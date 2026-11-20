# tests/

Plain-assert suites for the `kwikset_bench` harness -- no pytest, no hardware.
Each suite drives the real mode function (poll/poke/manual/confirm) against a
fake-MCU serial object and checks the console output and observed frames.
Promoted from the throwaway bench scripts; the package refactoring was gated
on these reproducing the monolith-era output byte-for-byte (modulo elapsed
times).

`tests/` is a namespace package (no `__init__.py`). Run a suite from
`source/` either way:

    ../.venv/bin/python -m tests.test_poll
    ../.venv/bin/python tests/test_poll.py

Every file bootstraps `sys.path` itself (parent-of-tests = `source/`), so
both styles work; each prints `PASS`/`FAIL` lines and a final summary
(`ALL ... PASSED` / `exit` non-zero if a check fails).

| file | covers | fake(s) |
|---|---|---|
| `fakes.py` | shared fake serial objects (not a suite) | -- |
| `test_confirm.py` | post-command state confirmation: immediate, flaky-skip, never-replies | local `FakeSerial` (the skip simulation is test-specific) |
| `test_manual.py` | `manual()` end-to-end: boot prefix, handshake, the simulated manual-op event must be flagged `*** MCU-INITIATED ***` | `McuFake(flip_at=None, silent=None, event_at=6.0)` |
| `test_poke.py` | `poke()` triage: fully deaf (all rounds silent), idle-but-awake (WAKE in round 2), deaf signature (cmd08/cmd09 only) | `FakeDeaf` / `FakeAsleep` / `FakeDeafSig` |
| `test_poll.py` | `poll()`: initial readout, `*** STATE CHANGE ***` flagging, `(unchanged)`, no-reply -> unknown, summary counts | `McuFake()` (default scenario: unlocked, flips to LOCKED at 8 s, cmd07-silent in [12,18)) |
| `test_poll_settings.py` | `poll --settings`: initial settings readout + `*** SETTINGS CHANGE ***` flagging | `McuFake(dip=0x05, dip_flip=0x04)` |
| `test_poll_with_poke.py` | `poll --with-poke`, five scenarios: silent timeout (no diagnostic), deaf diagnostic, session established via poke (exactly one 8-frame poke round), passive regression (prefix before the first poke, REQS from SEQ 01), prefix after a poke (SEQ resumes at 09) | `FakeDeaf`, `FakeDeafSig`, and `McuFake(lockstate=0x01, flip_at=None, silent=None, table=True, ...)` with `prefix_delay`/`prefix_repeat`/`awake_from` per scenario |

`fakes.py` notes:

* `Base` -- duck-typed pyserial stand-in: the harness only needs
  `write()`/`read()`/`close()`. Writes are buffered and BD-framed requests
  are parsed out of them (`tx` records every frame as role/cmd/seq);
  `read()` ticks timers first, so time-driven behavior works.
* `McuFake` -- live-ish bench MCU: boot prefix (AA cmd18 + AA cmd09 `[3C]`)
  with repeat/one-shot control, dynamic bench-style replies (cmd02 ID blob,
  cmd07 state readout, cmd08, cmd12 dip image when `dip` is set, cmd30 role
  55, cmd25 acks; no cmd09 reply in default mode), state/dip flips, the
  manual-op event, an asleep-until-`awake_from` gate, and `table=True`
  (reply per the fixed `BENCH_REPLIES` table instead -- the bench-variant
  values, cmd09 `0x64`, as the awake fakes need).
* `FakeDeaf` / `FakeAsleep` (acks from SEQ `0x09`) / `FakeDeafSig` (the
  11-20 post-autolock signature: cmd08 `00` + cmd09 `64` only).