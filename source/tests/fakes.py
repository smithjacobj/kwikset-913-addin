"""Shared fake-MCU serial objects for the tests/ suites (no hardware needed).

A fake serial is a duck-typed stand-in for the pyserial object the harness
talks to: the harness only needs write() / read() / close() on it. Fakes
accumulate what the harness writes, parse the BD-framed requests out of it,
run per-fake reply logic, and serve replies byte-by-byte from an internal
queue; read() ticks timers first, so time-driven behavior (boot prefixes,
state flips, event emission) works.

Run the suites from source/ with plain asserts (no pytest):

    ../.venv/bin/python -m tests.test_poll        # etc.
"""
import os
import sys
import time

# Make the kwikset_bench package importable no matter how the suite is
# invoked (`python -m tests.<name>` from source/, or `python tests/<name>.py`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kwikset_bench.frames import build_frame, split_frame

# Bench-board reply per request cmd: (role, data) -- the bench-variant
# values (cmd09 0x64, not the capture board's 0x46; fixed cmd07/cmd12
# payloads), for McuFake(table=True). cmd35: no reply, like the real board.
BENCH_REPLIES = {
    0x02: (0xAA, [0x08, 0x00, 0x45, 0x01]),
    0x07: (0xAA, [0x01]),
    0x08: (0xAA, [0x00]),
    0x09: (0xAA, [0x64]),
    0x30: (0x55, [0x03]),
    0x25: (0xAA, []),
    0x12: (0xAA, [0x03]),
}

class Base:
    """Plumbing shared by every fake: parse incoming frames on write(),
    serve queued reply bytes on read() (after _tick()). Subclasses implement
    _tick() (time-driven TX) and/or _on_frame(f) (reply to one received
    frame). Every frame received is recorded in self.tx as (role, cmd, seq).
    The default _on_frame acks (AA, empty data) every E7 frame once its SEQ
    reaches wake_at_seq (class attribute, None = never -- stays silent)."""
    wake_at_seq = None

    def __init__(self):
        self._rx = bytearray()
        self.out = bytearray()
        self.tx = []
        self.t0 = time.time()

    def _tick(self):
        pass

    def _on_frame(self, f):
        role, cmd, seq = f["fields"][2], f["fields"][3], f["fields"][1]
        if role != 0xE7:
            return
        if self.wake_at_seq is not None and seq >= self.wake_at_seq:
            self._rx += build_frame(seq, 0xAA, cmd, [])

    def write(self, frame):
        self.out += frame
        i = 0
        while i + 2 <= len(self.out):
            if self.out[i] != 0xBD:
                i += 1
                continue
            ln = self.out[i + 1]
            if len(self.out) < i + 2 + ln:
                break
            f = split_frame(list(self.out[i:i + 2 + ln]))
            i += 2 + ln
            if not f:
                continue
            self.tx.append((f["fields"][2], f["fields"][3], f["fields"][1]))
            self._on_frame(f)
        del self.out[:i]

    def read(self, n=1):
        self._tick()
        if self._rx:
            b = self._rx[:n]
            del self._rx[:n]
            return bytes(b)
        return b""

    def close(self):
        pass

class FakeDeaf(Base):
    """No boot prefix, never answers anything (fully dead-deaf)."""
    pass

class FakeAsleep(Base):
    """No boot prefix, answers nothing until its SEQ counter reaches
    wake_at_seq (0x09 = the second poke round's first frame)."""
    wake_at_seq = 0x09

class FakeDeafSig(Base):
    """The 11-20 post-autolock 'deaf' signature (PROTOCOL.md 14.12):
    answers ONLY cmd08 00 + cmd09 64, ignores everything else."""
    def _on_frame(self, f):
        role, cmd, seq = f["fields"][2], f["fields"][3], f["fields"][1]
        if role != 0xE7:
            return
        if cmd == 0x08:
            self._rx += build_frame(seq, 0xAA, 0x08, [0x00])
        elif cmd == 0x09:
            self._rx += build_frame(seq, 0xAA, 0x09, [0x64])

class McuFake(Base):
    """A live-ish bench MCU: boots with the AA cmd18 + AA cmd09 [3C] prefix
    (at t0 + prefix_delay; prefix_delay=None = never power-cycled) and, left
    unanswered, re-sends it every ~2.6 s (prefix_repeat=False = one-shot).
    Answers our E7 requests bench-style: cmd02 ID blob, cmd07 the current
    state byte, cmd08, cmd30 (role 55), cmd25 acks, and -- when dip is not
    None -- cmd12 with the current dipswitch image.

    Time-driven behavior:
      * flip_at / flip_state: at t0 + flip_at the state byte becomes
        flip_state (default 0x01 -- a simulated manual lock); dip_flip
        flips the dipswitch image at the same moment.
      * silent=(lo, hi): cmd07 replies are withheld for t in that window
        (the weak-battery no-reply path); None = never silent.
      * event_at: at t0 + event_at emit a simulated manual-op event
        (55 cmd2A 15 FF motor-complete + AA cmd03 echo-ack).
      * awake_from: until t0 + awake_from the fake is asleep (ignores all
        E7); the boot prefix still fires.
      * table: reply per the fixed BENCH_REPLIES table (full bench-style
        set, including cmd09 0x64 and cmd12 0x03) instead of the dynamic
        state/dip logic.
    """

    def __init__(self, lockstate=0x02, flip_state=0x01, flip_at=8.0,
                 silent=(12.0, 18.0), dip=None, dip_flip=None,
                 event_at=None, prefix_delay=0.0, prefix_repeat=True,
                 awake_from=None, table=False):
        super().__init__()
        self.lockstate = lockstate
        self.flip_state = flip_state
        self.flip_at = flip_at
        self.silent = silent
        self.dip = dip
        self.dip_flip = dip_flip
        self.event_at = event_at
        self.event_sent = False
        self.prefix_delay = prefix_delay
        self.prefix_repeat = prefix_repeat
        self.awake_from = awake_from
        self.table = table
        self.state = "boot" if prefix_delay is not None else "up"
        self.prefix_done = prefix_delay is None
        self.next_prefix = (self.t0 + prefix_delay) if prefix_delay is not None else 1e18

    def _awake(self):
        return self.awake_from is None or (time.time() - self.t0) >= self.awake_from

    def _tick(self):
        now = time.time()
        if self.state == "boot" and now >= self.next_prefix:
            self._rx += build_frame(0x01, 0xAA, 0x18, [0x00])
            self._rx += build_frame(0x02, 0xAA, 0x09, [0x3C])
            self.next_prefix = (now + 2.6) if self.prefix_repeat else 1e18
            if not self.prefix_repeat:
                self.state = "up"
        if self.flip_at is not None and now - self.t0 >= self.flip_at:
            if self.flip_state is not None:
                self.lockstate = self.flip_state
            if self.dip is not None and self.dip_flip is not None:
                self.dip = self.dip_flip
        if self.event_at is not None and not self.event_sent \
           and now - self.t0 >= self.event_at:
            self.event_sent = True
            self._rx += build_frame(0x40, 0x55, 0x2A, [0x15, 0xFF])
            self._rx += build_frame(0x41, 0xAA, 0x03, [])

    def _on_frame(self, f):
        role, cmd, seq = f["fields"][2], f["fields"][3], f["fields"][1]
        if role != 0xE7:
            return
        if not self._awake():
            return          # an asleep fake must not consume the boot prefix
        if self.state == "boot":
            self.state = "up"
            self.next_prefix = 1e18
        if self.table:
            exp = BENCH_REPLIES.get(cmd)
            if exp:
                self._rx += build_frame(seq, exp[0], cmd, exp[1])
            return
        if cmd == 0x02:
            self._rx += build_frame(seq, 0xAA, 0x02, [0x08, 0x00, 0x45, 0x01])
        elif cmd == 0x07:
            if self.silent is None or not (self.silent[0] <= time.time() - self.t0 < self.silent[1]):
                self._rx += build_frame(seq, 0xAA, 0x07, [self.lockstate])
        elif cmd == 0x08:
            self._rx += build_frame(seq, 0xAA, 0x08, [0x00])
        elif cmd == 0x12 and self.dip is not None:
            self._rx += build_frame(seq, 0xAA, 0x12, [self.dip])
        elif cmd == 0x30:
            self._rx += build_frame(seq, 0x55, 0x30, [0x03])
        elif cmd == 0x25:
            self._rx += build_frame(seq, 0xAA, 0x25, [])