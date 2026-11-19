#!/usr/bin/env python3
"""Kwikset 913/914 <-> Z-Wave Module UART bench harness (emulates the module).

CORRECTED DIRECTION (Nov 2026 rebuild): the 913 INITIATES. It transmits the boot
hello (role AA, cmd18) and, left unanswered, re-sends a hello+cmd09 retry prefix
every ~2.6 s. We (the module) answer by transmitting role=E7 requests; the 913
replies to each (role AA/55), echoing our request SEQ. See the PROTOCOL MODEL
section below for the full corrected role model.

Subcommands: sniff (passive listen), emulate (module steady state), act
(injection test), lock/unlock (drive one absolute state), manual (capture what
the MCU transmits when the lock is operated BY HAND -- the key path the Z-Wave
add-in board will also have to react to), calibrate/verify (CRC vs captures)."""
import argparse, sys, os, struct, time

# ---------- CRC-8 (parametric, reflected/non-reflected) ----------
def crc8(data, poly, init=0x00, reflected=False, xorout=0x00):
    crc = init
    for b in data:
        if reflected:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ (poly if (crc & 1) else 0)
        else:
            crc ^= (b << 8) & 0xFF
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & 0xFF
    return crc ^ xorout

def build_crc32table(poly):
    t = []
    for i in range(256):
        t.append(crc8(bytes([i]), poly, init=0, reflected=True, xorout=0))
    return t

def crc8_fast(data, poly, init=0x00, reflected=True, xorout=0x00):
    t = build_crc32table(poly)
    crc = init
    for b in data:
        crc = t[crc ^ b]
    return crc ^ xorout

# ---------- confirmed CRC-8 model (full 256-poly calibrate winner) ----------
# check = crc8([LEN, SEQ, ROLE, CMD, DATA...], poly=0x80, reflected, init=0xFF, xorout=0x00)
CRC_POLY, CRC_INIT, CRC_XOR = 0x80, 0xFF, 0x00
def build_frame(seq, role, cmd, data=b""):
    """Build [BD][LEN][SEQ][ROLE][CMD][DATA...][CHECK] with a verified check byte."""
    data = bytes(data)
    ln = 4 + len(data)                      # LEN counts SEQ,ROLE,CMD,DATA,CHECK
    body = bytes([seq, role, cmd]) + data
    check = crc8(bytes([ln]) + body, CRC_POLY, init=CRC_INIT, reflected=True, xorout=CRC_XOR)
    return bytes([0xBD, ln]) + body + bytes([check])

# ---------- CSV capture parsing ----------
def parse_csv(path):
    """Returns list of (line_name, t_float_seconds, byte_int) sorted by time."""
    rows = []
    with open(path) as f:
        header = f.readline()
        for line in f:
            line = line.strip()
            if not line:
                continue
            # format: "RXD1","data",<ts>,<dur>,<data>
            parts = line.split(",")
            if len(parts) < 5:
                continue
            name = parts[0].strip('"')
            # data may be quoted
            data = parts[4].strip().strip('"')
            if data.startswith("0x") or data.startswith("0X"):
                b = int(data, 16)
            else:
                b = int(data)
            # timestamp ISO8601; use monotonically increasing order from file
            rows.append((name, b, line))
    return rows

def reconstruct(rows):
    """Group bytes into frames per direction. Returns dict name->list of parsed
    frame dicts (or None for truncated frames). See split_frame."""
    from collections import OrderedDict
    frames = OrderedDict()
    for name in dict.fromkeys(n for n,_,_ in rows):
        seq = [(b,) for (n,b,_) in rows if n == name]
        out = []
        i = 0
        while i < len(seq):
            if seq[i][0] == 0xBD:
                # start frame; next byte = LEN
                if i+1 >= len(seq):
                    break
                ln = seq[i+1][0]
                block = [seq[i][0], seq[i+1][0]]
                # take len bytes after (seq,role,cmd,data...)
                j = i+2
                while len(block) < 2 + ln and j < len(seq):
                    block.append(seq[j][0]); j += 1
                out.append(split_frame(block))
                i = j
            else:
                i += 1
        frames[name] = out
    return frames

def split_frame(block):
    if len(block) < 2 or block[0] != 0xBD:
        return None
    ln = block[1]
    if len(block) < 2 + ln:
        return None  # truncated
    body = block[2:2+ln]           # [seq, role, cmd, data..., check], ln bytes
    check = body[-1]
    # fields = [LEN, SEQ, ROLE, CMD, DATA...]  (LEN prepended, check excluded)
    fields = [ln] + list(body[:-1])
    return {"len": ln, "fields": fields, "check": check}

# ---------- candidate CRC models for calibration ----------
POLYS = [0x80,0x07,0x09,0x31,0x39,0x9B,0x2F,0x49,0x8D,0xA7,0xD5,0xFD,0x1D,0xAB,0xB9,0x1F,0xFF,0x55,0xE7]
def calibrate(frames):
    """Try models x ranges against every frame; report best match counts."""
    allf = [f for fs in frames.values() for f in fs if f]
    if not allf:
        print("No complete frames found."); return
    # candidate ranges: which fields the CRC covers
    ranges = {
        "LEN..DATA":     lambda f: f["fields"],                  # [LEN,SEQ,ROLE,CMD,DATA...]
        "SEQ..DATA":     lambda f: f["fields"][1:],             # drop LEN
        "ROLE..DATA":    lambda f: f["fields"][2:],
        "CMD..DATA":     lambda f: f["fields"][3:],
        "fields+check?": lambda f: f["fields"] + [f["check"]],  # sanity: check in input
    }
    best = None
    for rname, rf in ranges.items():
        for poly in POLYS:
            for refl in (True, False):
                for init in (0x00, 0xFF):
                    for xorout in (0x00, 0xFF):
                        ok = 0
                        for f in allf:
                            d = rf(f)
                            if not d:
                                continue
                            if crc8(d, poly, init=init, reflected=refl, xorout=xorout) == f["check"]:
                                ok += 1
                        if best is None or ok > best[0]:
                            best = (ok, rname, hex(poly), refl, hex(init), hex(xorout))
    print(f"Total complete frames: {len(allf)}")
    if best and best[0] > 0:
        ok, rname, poly, refl, init, xorout = best
        status = "WINNER (matches all)" if ok == len(allf) else f"partial {ok}/{len(allf)}"
        print(f"{status}: range={rname} poly={poly} reflected={refl} init={init} xorout={xorout}")
    else:
        print("No plain CRC-8 model reproduced any check byte. Check may be sum/XOR/custom.")
        # quick alternative tests
        test_sum_xor(allf)

def test_sum_xor(frames):
    allf = [f for fs in frames.values() for f in fs if f]
    for label, rf in [("sum fields", lambda f: f["fields"]),
                      ("xor fields", lambda f: f["fields"])]:
        ok = 0
        for f in allf:
            d = rf(f)
            s = sum(d) & 0xFF
            x = 0
            for b in d: x ^= b
            if s == f["check"] or x == f["check"]:
                ok += 1
        print(f"  alt {label}: {ok}/{len(allf)} (sum or xor)")

def frame_text(f):
    fl = f["fields"]
    # fl = [len, seq, role, cmd, *data]
    ln, seq, role, cmd = fl[0], fl[1], fl[2], fl[3]
    data = fl[4:]
    return (f"len={ln:2d} seq={seq:02X} role={role:02X} cmd={cmd:02X} "
            f"data={' '.join('%02X'%b for b in data):<12} check={f['check']:02X}")

# ---------- verbose logging ----------
VERBOSE = False

def _ts():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + (".%03d" % int((t % 1) * 1000))

def crc_valid(f):
    """True if the frame's check byte matches the confirmed CRC model.
    The CRC runs over [LEN,SEQ,ROLE,CMD,DATA...] which is exactly f['fields']
    (f['fields'] already carries the LEN byte as its first element — do NOT
    prepend f['len'] again, that double-counts LEN and flags every frame BAD)."""
    d = bytes(f["fields"])
    return crc8(d, CRC_POLY, init=CRC_INIT, reflected=True, xorout=CRC_XOR) == f["check"]

def fmt_hex(block):
    return ' '.join('%02X' % x for x in list(block))

def log_frame(dirn, block, label=""):
    """Log one wire frame. dirn is '<-' (RX) / '->' (TX) / '>>' (inject).
    Normal mode prints parsed frames concisely; verbose (-v) adds a timestamp,
    a CRC ok/BAD flag, and dumps raw hex for anything that doesn't frame up."""
    b = list(block)
    f = split_frame(b)
    suffix = ("  " + label) if label else ""
    ts = ("[%s] " % _ts()) if VERBOSE else ""
    if f is None:
        if VERBOSE:
            print("  %s%sRAW  %s  (unframed)%s" % (dirn, ts, fmt_hex(b), suffix))
        return None
    crc = ("  %s" % ("crc=ok" if crc_valid(f) else "crc=BAD")) if VERBOSE else ""
    print("  %s%s%s%s%s" % (dirn, ts, frame_text(f), crc, suffix))
    return f

def tx(s, frame, label=""):
    """Send a frame; log it. Verbose shows the actual bytes + CRC validity."""
    s.write(frame)
    if VERBOSE:
        log_frame("->", frame, label)
    else:
        print("  -> %s" % (label or "sent"))

# ---------- live UART ----------
def open_port(path, baud=9600, swap=False):
    import serial
    s = serial.Serial(port=path, baudrate=baud, bytesize=8, parity='N',
                      stopbits=serial.STOPBITS_ONE, timeout=0.2)
    time.sleep(1.2)   # let DTR settle / MCU boot
    s.flushInput(); s.flushOutput()
    return s

# =====================================================================
# PROTOCOL MODEL — CORRECTED DIRECTION  (rebuild, Nov 2026)
# =====================================================================
# The primer's role model had EVERY direction flipped. Settled on this board
# by a live A/B (probe_e7.py): the 913 answers our role=E7 frames and ignores
# our role=AA frames. So the role byte marks direction, not just type:
#
#   ROLE  0xE7   module -> MCU   (requests)        <- WE transmit these
#   ROLE  0xAA   MCU -> module   (responses)       <- the 913 transmits these
#   ROLE  0x55   MCU -> module   (cmd / async)     <- the 913 transmits these
#
# Handshake (corrected): the MCU INITIATES. It transmits the hello (AA cmd18)
# and, left unanswered, re-sends a hello+cmd09 retry prefix every ~2.6 s (SEQ
# resets to 01 each retry). The module (us) answers by transmitting its request
# sequence (E7); the 913 replies to each request (AA/55), echoing our request's
# SEQ. Steady state: we periodically send cmd25 (E7, a status report); the 913
# acks it (AA, empty data — the varying byte is the SEQ-dependent CHECK, not a
# data byte).
#
# NOTE: this board is a DIFFERENT REVISION than start-unlock-lock.csv — same
# command set + SEQ-echo, but board-specific payloads (see EXPECTED below). The
# CSV's old hardcoded values (id 04 00 36 0D, cmd09 5A, cmd30 01/AA, cmd35
# 16x20) do NOT match this board and are gone.
# =====================================================================
ROLE_MOD    = 0xE7   # module -> MCU : requests (what WE send)
ROLE_MCU_AX = 0xAA   # MCU -> module : responses to our requests
ROLE_MCU_CMD= 0x55   # MCU -> module : initiated commands / async events

# The MCU's boot hello — our sync point. (role, cmd, data)
MCU_HELLO = (0xAA, 0x18, [0x00])

# Our request sequence (all role=E7), sent once at handshake. (cmd, data)
REQS = [
    (0x02, []),               # readID
    (0x07, []),
    (0x08, []),
    (0x09, []),
    (0x35, []),               # config — this board: no reply
    (0x30, []),
]
# Steady-state poll, sent periodically. (cmd, data) — a module status report.
STEADY_REQ = (0x25, [0x27, 0x00])

# Expected 913 replies for THIS board, keyed by our request cmd: (role, data).
# Validation/logging only — the 913 sends these, we don't. (No 0x35 entry: this
# board does not reply to the config request.)
EXPECTED = {
    0x02: (0xAA, [0x08, 0x00, 0x45, 0x01]),  # ID/cap blob (board-specific)
    # 0x07: state-dependent -- handled in _check_expected (01 locked /
    # 02 unlocked on this bench board; the capture board replies 00).
    0x08: (0xAA, [0x00]),
    0x09: (0xAA, [0x46]),  # in-dialogue reply. The MCU-initiated BOOT prefix
                           # cmd09 byte varies instead: 3C (18th 10:37) and 50
                           # (18th 11:29, unstable/low-battery boots), 46 (18th
                           # 14:13, low batteries), 64 (18th 14:52-15:02, fresh
                           # batteries). Undecoded; no effect on actuation.
    0x30: (0x55, [0x03]),
    0x25: (0xAA, []),
}

def _d(data):
    return " ".join("%02X" % x for x in data) or "-"

# cmd07 reply data byte -> human state (bench board: 01 locked / 02 unlocked;
# the capture board replies 00). Shared by _check_expected and _confirm_state.
# 00 "unknown" = no conclusive closed-loop state: observed on the bench board
# in the 11-18 10:35/36 unstable/low-battery runs, where the motor apparently
# stalled short of full travel (user's hypothesis: no end-stop -> no
# conclusive state). The 18th 14:14 INCONCLUSIVE (MCU skipped all 3 confirm
# replies) is likely the same condition expressed as silence. Never seen with
# fresh batteries (5/5 clean readouts the 18th afternoon).
STATE_WORDS = {0x01: "LOCKED", 0x02: "unlocked"}
def _state_word(b):
    return STATE_WORDS.get(b, "unknown")

def send_reqs(s, seq):
    """Transmit our module request sequence (all role=E7). Returns next free SEQ."""
    for cmd, data in REQS:
        tx(s, build_frame(seq, ROLE_MOD, cmd, data),
           "req cmd%02X %s seq=%02X" % (cmd, _d(data), seq))
        seq = (seq + 1) & 0xFF
    return seq

# =====================================================================
# HANDSHAKE + VALIDATION  (corrected direction)
# =====================================================================
# The MCU initiates; we answer with E7 requests; it replies (AA/55) echoing our
# SEQ. These helpers implement exactly that flow. (They replace the old
# backwards RESPS/make_reply model, where WE answered the MCU's requests.)

def _check_expected(f):
    """Validate an incoming 913 frame against this board's expected reply.
    Silent on a match; one warning line if role or data differ from EXPECTED.
    cmd07 is special: its reply data byte is the lock-state readout (01 locked
    / 02 unlocked on this bench board; 00 on the capture board) -- logged as
    info, never as a mismatch."""
    cmd, role, data = f["fields"][3], f["fields"][2], f["fields"][4:]
    if cmd == 0x07:
        state = _state_word(data[0] if data else None)
        print("      ^ cmd07 reply data=%s -> lock state: %s" % (_d(data), state))
        return
    exp = EXPECTED.get(cmd)
    if exp is None:
        return
    e_role, e_data = exp
    if role != e_role or data != e_data:
        print("      ^^ MISMATCH cmd%02X: want role=%02X data=%s, got role=%02X data=%s"
              % (cmd, e_role, _d(e_data), role, _d(data)))

def _wait_prefix(s, timeout=15.0, after="cmd09"):
    """Wait for the 913's boot prefix. The MCU INITIATES: it transmits the hello
    (AA cmd18), then a cmd09 retry frame, and re-sends the pair every ~2.6 s
    (SEQ resets to 01) until it gets a valid module answer. Returns True when
    the requested sync point (`after`: 'hello' or 'cmd09') is seen within
    `timeout`. Every frame is logged (verbose-aware) as it arrives."""
    saw = {"hello": False, "cmd09": False}
    start = time.time()
    while time.time() - start < timeout:
        b = recv_frame(s, timeout=1.0)
        if not b:
            continue
        f = log_frame("<-", b)
        if not f:
            continue
        role, cmd = f["fields"][2], f["fields"][3]
        if role == ROLE_MCU_AX and cmd == 0x18:
            saw["hello"] = True
        if role == ROLE_MCU_AX and cmd == 0x09:
            saw["cmd09"] = True
        if (after == "hello" and saw["hello"]) or \
           (after == "cmd09" and saw["hello"] and saw["cmd09"]):
            return True
    return False

def _do_handshake(s, hello_timeout=15.0, after="cmd09"):
    """Corrected handshake: wait for the MCU's prefix, then transmit our E7
    request sequence. Returns the next free SEQ, or None if no prefix was seen
    (e.g. the enable pin is not driven High)."""
    want = "hello" if after == "hello" else "hello+cmd09"
    print("Waiting for the 913's boot prefix (%s)..." % want)
    if not _wait_prefix(s, timeout=hello_timeout, after=after):
        print("No prefix in %.0fs. Is the enable pin tied High and the 913 power-cycled?" % hello_timeout)
        print("  (the boot prefix is sent ONLY at power-on: power-cycle AFTER this script started)")
        return None
    print("Prefix seen -> transmitting our E7 request sequence.")
    return send_reqs(s, 0x01)     # 913 echoes our SEQ in replies (match probe_e7.py)

# =====================================================================
# ACTUATION CLUSTER
# =====================================================================
# Actuation frames from the CSV capture (different board revision).
#
# LIVE-VALIDATED 2026-11-16/17 (bench 913, each test starting UNLOCKED):
#   * --roles as-captured (original 55/AA bytes): the MCU ignored everything
#     (negative). It only acts on the E7 (module->MCU) direction.
#   * Full cluster as E7 (16th 15:34): lock ACTUATED; MCU sent 55 cmd4E
#     data03 (the cmd4E ack, not the motor event).
#   * cmd4E ALONE (16th 15:48): MCU acked but the motor did NOT move
#     -> cmd4E is ack-only, NOT the motor trigger.
#   * Cluster MINUS cmd4E (16th 16:03, frames 0,2,3,4,5,6): ACTUATED, no 55
#     frames (silent execution — the dialogue frames led the burst).
#   * cmd09 5A + cmd07 00 alone (16th 16:10, frames 0,2): the MCU replied
#     AA to both but the lock did NOT move -> dialogue pair ruled out; it
#     only converses, never executes.
#   * cmd2A 14 + cmd03, DUPLICATED (16th 16:39, frames 3,4,5,6): ACTUATED;
#     MCU replied 55 cmd2A data03 + 55 cmd03 (motor frames led the burst).
#   * cmd2A 14 + cmd03, SINGLE COPY (17th 10:43, frames 3,4): ACTUATED
#     -> duplicates are NOT required; the single ordered pair is the minimal
#     known trigger. Wire: 55 cmd2A data03 (+32 ms), then a delayed 55 cmd2A
#     data=15 FF (~2.9 s later, seq=03) — likely motor-complete; 1 sample,
#     undecoded. The 55 cmd03 reply appeared only with the duplicated pair.
#   * cmd2A 14 ALONE (17th 11:02, frame 3): MCU acked 55 cmd2A data03 but the
#     lock did NOT move -> cmd2A is ack-only (like cmd4E), NOT the trigger.
#   * cmd03 ALONE (17th 11:08 AND 11:21, frame 4, 2 samples): the lock MOVED
#     both times, yet the MCU did NOT ack it (no 55, no AA cmd03 — only the
#     periodic cmd25 acks). CONFIRMED reproducible. -> cmd03 is the motor
#     trigger, NOT cmd2A.
#   * CORRELATION (all 7 act tests): every run that ACTUATED contained cmd03;
#     every run that did NOT (cmd09+cmd07, cmd2A alone) lacked it. cmd03
#     presence is the differentiator — the 55 ack is NOT a reliable actuation
#     indicator (cmd4E/cmd2A are acked but inert; cmd03 actuates, often
#     un-acked). Physical observation is the ground truth.
#   * Open (lock semantics): cmd03 is ZERO-DATA, so the trigger carries no
#     payload -- does it LOCK, UNLOCK, or TOGGLE? All 7 tests started manually
#     UNLOCKED and drove to LOCKED. Next experiment: a LOCKED-start run (user
#     locks, then power-cycles with the script running). ASSUMPTION to verify:
#     does the MCU's internal bolt state (or a boot position-sense) survive a
#     power-cycle? If not, the MCU may believe "unlocked" even when the bolt is
#     in, confounding the toggle-vs-absolute read of the result.
#   * Open (data bytes, now lower priority): what cmd2A data14 / cmd4E data04 /
#     cmd09 data5A encode; reply data03 appears on BOTH the cmd4E and cmd2A
#     acks -> a generic ack/status code, not a state value.
#
# CAPTURE-DERIVED MODEL (captures/start-*.csv: real Z-Wave module + this MCU,
# board ID 04 00 36 0D; same sniffer session as start-unlock-lock.csv):
#   * 12.06 s after the handshake the MCU spontaneously sends E7 cmd09 data5A
#     (a prompt); the module replies cmd4E; the MCU acks 55 cmd4E data04.
#   * Per actuation event the module sends BOTH state commands, target LAST:
#     from locked: cmd03 (no-op reseat), cmd05 (unlock); from unlocked:
#     cmd05 (no-op), cmd03 (lock) -- interleaved with cmd07 status polls
#     (~6.3 s apart). The MCU's async 55 cmd2A data14 = "motor complete,"
#     arriving a fixed ~10.5 s after EVERY state command (no-op included),
#     followed ~60 ms later by an AA echo-ack of the LAST state command.
#     The module waits for the complete event before issuing the next command.
#   => cmd03 = absolute LOCK (extend bolt), cmd05 = absolute UNLOCK (retract);
#      each motor-runs to the commanded end state even when already there.
#      Matches all 7 bench tests (unlocked start + cmd03 -> locked).
# BENCH-CONFIRMED BOTH DIRECTIONS (17th):
#   * cmd05 alone from LOCKED (13:37): bolt RETRACTED (unlocked). MCU sent
#     55 cmd2A data=15 FF ~3.9 s after injection (bench motor-complete);
#     no AA echo-ack this time (bench acks stay flaky).
#   * cmd03 alone from LOCKED (11:47): NO-MOVE no-op reseat -- confirmed by
#     the 13:37 handshake state readout (cmd07 reply 01 = still LOCKED at
#     the next boot).
#   * BENCH motor-complete = 55 cmd2A data 15 FF (~3-4 s after actuation;
#     2 samples: 10:43 pair run, 13:37 cmd05) vs the capture board's
#     55 cmd2A data 14 at ~10.5 s. It was absent in ALL 8 afternoon 18th
#     runs -- the confirm then runs via the CONFIRM_FALLBACK path and still
#     PASSES. Status LED stays OFF after our bench commands (real module
#     keeps it blinking); re-confirmed 18th on the healthy unit -- it
#     returns to blinking only after PHYSICAL (key) manipulation. Noted,
#     still deferred.
#   * CONFIDENCE RE-VALIDATION (18th afternoon, fresh batteries, better-
#     assembled unit): 5/5 PASS -- 14:52 unlock reseat (02), 14:53 lock
#     (02->01), 14:54 unlock (01->02), 14:54 lock (02->01), 15:02 lock
#     (02->01). Every confirm reply on attempt 1/3; every handshake readout
#     consistent with the previous run's end state (state survives a
#     power-cycle). The 10:35/10:36 FAILs (readback 02, then 00 "unknown")
#     were low-battery/unstable-state artifacts: the motor apparently stalled
#     short of full travel, so the closed-loop readout had no conclusive
#     state (00 "unknown", or silence; see the STATE_WORDS note). Same-day
#     low-battery runs: 14:13 PASS (no-op reseat), 14:14 INCONCLUSIVE (MCU
#     skipped all 3 confirm replies -- likely the same condition, expressed
#     as silence), 14:50 total silence (no boot prefix at all).
#   => cmd07 = status poll; its reply data byte is the state readout on the
#      bench board (01 locked / 02 unlocked; the capture board replies 00).
#
# CHECK bytes are computed by build_frame(); SEQ is assigned at run time.
#
# Each entry: (role, cmd, data, capture_seq).
ACT_CLUSTER = [
    (0xE7, 0x09, [0x5A], 0x02),  # req  cmd09 data5A   (dialogue only; no actuation)
    (0x55, 0x4E, [0x04], 0x0C),  # cmd  cmd4E data04   (module reply to the MCU's cmd09 5A prompt; ack-only)
    (0xAA, 0x07, [0x00], 0x0E),  # resp cmd07 data00   (status poll; reply byte = state readout)
    (0x55, 0x2A, [0x14], 0x03),  # cmd  cmd2A data14   (MCU "motor complete" async event, ~10.5s after a state cmd)
    (0xAA, 0x03, [],     0x0F),  # resp cmd03          (THE trigger: confirmed alone, x2, un-acked)
    (0x55, 0x2A, [0x14], 0x04),  # cmd  cmd2A data14 (repeat; not required)
    (0xAA, 0x03, [],     0x0F),  # resp cmd03 (repeat; not required)
    (0xE7, 0x05, [],     0x0F),  # req  cmd05 no data  (UNLOCK: bench-confirmed 17th 13:37 from locked)
]

def recv_frame(s, timeout=2.0):
    """Read bytes until we can parse one frame; return block or None."""
    buf = b""
    start = time.time()
    while time.time() - start < timeout:
        c = s.read(1)
        if not c:
            continue
        buf += c
        if buf[0] != 0xBD:
            if len(buf) > 64: buf = c  # resync
            continue
        if len(buf) >= 2:
            need = 2 + buf[1]
            if len(buf) >= need:
                return list(buf[:need])
    return list(buf) if buf and buf[0]==0xBD else None

def emulate(s, seconds=30, hello_timeout=15.0, after="cmd09", poll_every=2.0):
    print("Emulate (corrected direction): wait for the 913's prefix, send our E7 "
          "request sequence, then log its replies and send periodic cmd25 status "
          "reports for %ds. Ctrl-C to stop." % seconds)
    seq = _do_handshake(s, hello_timeout=hello_timeout, after=after)
    if seq is None:
        return
    end = time.time() + seconds
    last_poll = 0.0
    while time.time() < end:
        b = recv_frame(s, timeout=1.0)
        if b:
            f = log_frame("<-", b)
            if f:
                _check_expected(f)
        if poll_every and time.time() - last_poll >= poll_every:
            rcmd, rd = STEADY_REQ
            tx(s, build_frame(seq, ROLE_MOD, rcmd, rd),
               "status cmd25 %s seq=%02X" % (_d(rd), seq))
            seq = (seq + 1) & 0xFF
            last_poll = time.time()

def _next_seq(seqmode, base, i, capseq):
    """Assign the SEQ for cluster frame i.
      'track'   -> base+1+i : next-available after the highest SEQ seen from the MCU.
                     This is the fix for the stale 0x0C injection (primer gotcha #7).
      'capture' -> capseq    : exact SEQ from capture 3, to reproduce it byte-for-byte.
      'zero'    -> 0x00      : probe whether the MCU checks SEQ at all.
    """
    if seqmode == "capture":
        return capseq
    if seqmode == "zero":
        return 0x00
    return (base + 1 + i) & 0xFF

def _cluster_idx(cmd, data=b""):
    """First ACT_CLUSTER index whose (cmd, data) match (for lock/unlock)."""
    for i, (role, c, d, capseq) in enumerate(ACT_CLUSTER):
        if c == cmd and d == list(data):
            return i
    raise SystemExit("no ACT_CLUSTER entry for cmd%02X data=%s" % (cmd, _d(data)))

def _parse_frames(spec):
    """Parse a --frames spec like '0,2,3,4,5,6' into cluster indices."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        i = int(part)
        if not 0 <= i < len(ACT_CLUSTER):
            raise SystemExit("--frames index %d out of range 0..%d"
                             % (i, len(ACT_CLUSTER) - 1))
        out.append(i)
    return out or None

def _inject(s, mode, seqmode, base, roles="as-captured", frames=None):
    """Fire the actuation.
      mode 'single' = one cmd4E frame (isolation A/B).
      mode 'cluster' = the observed ACT_CLUSTER, or the --frames subset of it,
        per-frame SEQ per seqmode.
      roles 'as-captured' = the cluster's original role bytes; 'e7' = force every
        frame to role=E7 (the direction WE send)."""
    if mode == "single":
        q = _next_seq(seqmode, base, 0, 0x0C)
        r = ROLE_MOD if roles == "e7" else 0x55
        tx(s, build_frame(q, r, 0x4E, [0x04]),
           "INJECT cmd4E data04 seq=%02X role=%02X" % (q, r))
        return
    idxs = frames if frames is not None else range(len(ACT_CLUSTER))
    for i in idxs:
        role, cmd, data, capseq = ACT_CLUSTER[i]
        if roles == "e7":
            role = ROLE_MOD
        q = _next_seq(seqmode, base, i, capseq)
        d = ' '.join('%02X' % x for x in data) or "-"
        tx(s, build_frame(q, role, cmd, data),
           "INJECT %02X %s seq=%02X role=%02X" % (cmd, d, q, role))

def _print_act_summary(baseline, post):
    """End-of-run report: which (role,cmd) the MCU sent before vs after the
    injection, and whether anything NEW appeared after it."""
    new = sorted(set(post) - set(baseline))
    print("\n=== ACT SUMMARY ===")
    print("  pre-injection baseline (role,cmd):count")
    for rc, n in sorted(baseline.items()):
        print("    role=%02X cmd=%02X  x%-3d" % (rc[0], rc[1], n))
    print("  post-injection (role,cmd):count")
    for rc, n in sorted(post.items()):
        tag = "   *** NEW" if rc not in baseline else ""
        print("    role=%02X cmd=%02X  x%-3d%s" % (rc[0], rc[1], n, tag))
    if new:
        print("  -> NEW frame(s) after injection: %s"
              % ", ".join("role=%02X/cmd=%02X" % rc for rc in new))
        print("     The MCU reacted to the injection (it never sent these before).")
    else:
        print("  -> no NEW frame after injection; only repeats of baseline frames.")
        print("     Still check: did it BEEP or did the lock move? A motor action")
        print("     with no new frame = an internal reaction (the MCU acted but")
        print("     sent no distinct frame). Note: the validated trigger direction")
        print("     is --roles e7 (module->MCU); as-captured 55/AA are ignored.")

# Post-command state confirmation (confidence reps). After a lock/unlock command
# the motor settles when the MCU emits its async motor-complete (55 cmd2A); we
# then poll cmd07 and read the state byte back, comparing it to the state we
# commanded. Timing: prefer the motor-complete + a short settle; if the (flaky)
# complete never arrives, fall back to a fixed post-injection wait.
CONFIRM_SETTLE   = 1.0    # s after the motor-complete before reading state
CONFIRM_FALLBACK = 10.0   # s after injection if no complete is seen (bench motor
                          # completes ~3-4 s; the capture board's ~10.5 s is the
                          # conservative reference)

def _confirm_state(s, seq, retries=3, per_retry=2.0):
    """Send a cmd07 status poll and read the state byte back, retrying if the MCU
    skips the reply (known flaky, 3 samples). Returns (state_byte or None,
    next_seq). cmd07 is a read (role E7) and never actuates."""
    for attempt in range(1, retries + 1):
        q = seq & 0xFF
        seq = (seq + 1) & 0xFF
        tx(s, build_frame(q, ROLE_MOD, 0x07, []),
           "CONFIRM cmd07 seq=%02X (attempt %d/%d)" % (q, attempt, retries))
        start = time.time()
        while time.time() - start < per_retry:
            b = recv_frame(s, timeout=0.5)
            if not b:
                continue
            f = log_frame("<-", b)
            if not f:
                continue
            if f["fields"][2] == ROLE_MCU_AX and f["fields"][3] == 0x07:
                data = f["fields"][4:]
                if data:
                    return data[0], seq
                # no data byte yet; keep waiting this attempt
    return None, seq

def act(s, seconds=30, settle=8, mode="cluster", seqmode="track",
        hello_timeout=15.0, after="cmd09", poll=2.0, roles="as-captured",
        frames=None, label=None, confirm_state=None):
    what = label if label is not None else (mode if frames is None else "subset %s" % frames)
    print("Act (corrected direction): handshake, then inject %s (roles=%s) after "
          "%ds settle (seq=%s, poll=%ss). Ctrl-C to stop."
          % (what, roles, settle, seqmode, poll))
    print("  Watch for: a BEEP + any *** NEW *** post-injection frame + the lock itself.")
    if confirm_state:
        print("  CONFIRM: after the motor settles it polls cmd07 and reports the state")
        print("  readback vs the commanded state (%s) -- PASS/FAIL at the end."
              % confirm_state[1])
    print("  MODEL (bench 11-16/17 + captures, both directions confirmed):")
    print("  cmd03 = LOCK, cmd05 = UNLOCK (absolute; each motor-runs even when")
    print("  already in state). cmd09/cmd07/cmd2A/cmd4E are dialogue/ack-only.")
    print("  cmd07 reply data byte = state readout (01 locked / 02 unlocked).")
    seq = _do_handshake(s, hello_timeout=hello_timeout, after=after)
    if seq is None:
        return
    end = time.time() + seconds
    settle_at = time.time() + settle
    last_poll = 0.0
    injected = False
    inject_time = 0.0
    saw_complete = False
    complete_time = 0.0
    confirmed = False
    max_seq = 0x00
    baseline = {}      # (role,cmd) -> count, seen BEFORE the injection
    post = {}          # (role,cmd) -> count, seen AFTER the injection

    def _do_confirm():
        """Read the state back once the motor has settled; report PASS/FAIL."""
        nonlocal confirmed, seq
        print("\n>>> === CONFIRM: reading state back (cmd07) ===")
        state, seq = _confirm_state(s, seq)
        if state is None:
            print("  ==> CONFIRM: no cmd07 reply -> INCONCLUSIVE (trust the bolt + LED)")
        else:
            ok = (state == confirm_state[0])
            print("  ==> RESULT: read data=%s -> %s   [%s]   (commanded %s)"
                  % (_d([state]), _state_word(state),
                     "PASS" if ok else "FAIL", confirm_state[1]))
        confirmed = True

    try:
        while time.time() < end:
            b = recv_frame(s, timeout=1.0)
            if b:
                f = log_frame("<-", b)
                if f:
                    rc = (f["fields"][2], f["fields"][3])
                    bucket = post if injected else baseline
                    bucket[rc] = bucket.get(rc, 0) + 1
                    max_seq = max(max_seq, f["fields"][1])     # fields[1] = SEQ
                    _check_expected(f)
                    if injected:
                        if rc == (ROLE_MCU_CMD, 0x2A) and not saw_complete:
                            saw_complete = True               # motor-complete seen
                            complete_time = time.time()
                        if rc not in baseline:
                            print("    *** NEW post-injection frame: role=%02X cmd=%02X" % rc)
                        elif rc[0] == ROLE_MCU_CMD:
                            print("    (55 async frame, post-injection)")
            # keep steady state calm: periodic module status report (E7 cmd25)
            if poll and time.time() - last_poll >= poll:
                rcmd, rd = STEADY_REQ
                tx(s, build_frame(seq, ROLE_MOD, rcmd, rd),
                   "status cmd25 %s seq=%02X" % (_d(rd), seq))
                seq = (seq + 1) & 0xFF
                last_poll = time.time()
            if not injected and time.time() >= settle_at:
                print("\n>>> === INJECTING %s roles=%s (seq=%s, base=%02X) ==="
                      % (what, roles, seqmode, max_seq))
                _inject(s, mode, seqmode, max_seq, roles, frames)
                print(">>> === POST-INJECTION WATCH: listen for a BEEP ===\n")
                injected = True
                inject_time = time.time()
            # confidence rep: once the motor settles, read the state back
            if confirm_state and injected and not confirmed:
                due = (saw_complete and time.time() - complete_time >= CONFIRM_SETTLE) \
                      or (time.time() - inject_time >= CONFIRM_FALLBACK)
                if due:
                    _do_confirm()
    except KeyboardInterrupt:
        pass
    # best effort: if the loop ended before the confirm fired, run it once
    if confirm_state and injected and not confirmed:
        _do_confirm()
    _print_act_summary(baseline, post)

def sniff(s, seconds=30):
    print("Passive sniff for %ds (shows which pin carries which direction). Ctrl-C to stop." % seconds)
    print("(run with -v to also log raw/unframed bytes, timestamps, and CRC ok/BAD)")
    end = time.time() + seconds
    while time.time() < end:
        b = recv_frame(s, timeout=1.0)
        if b:
            log_frame("<-", b)

# =====================================================================
# MANUAL-OP CAPTURE  (lock operated BY HAND -> what does the MCU transmit?)
# =====================================================================
# For the Z-Wave add-in board the module must react to manual (key) ops, so
# capture what the MCU puts on the UART when the bolt moves without a UART
# command. Flow: handshake (like act), then listen for --seconds:
#   * Every incoming frame is logged. A frame matching the (seq,cmd) of one of
#     OUR requests is a reply to us (silent for cmd25 poll acks); anything else
#     is flagged *** MCU-INITIATED *** -- the manual-event candidates.
#   * As the user FINISHES a manual op they type a label ('lock'/'unlock') +
#     Enter in the console: a MARK line is stamped and we send one cmd07 state
#     readout (the state the MCU believes, logged by _check_expected). This
#     yields a state signal even if the MCU stays silent on manual ops.
#   * >10 s between MCU frames prints a quiet marker.
# Start this script FIRST, then power-cycle the lock (boot prefix is power-on
# only). --poll 0 = fully quiet listen (the session may idle out -- the hello
# retry loop would then be visible as MCU-initiated).
def manual(s, seconds=120, hello_timeout=15.0, after="cmd09", poll=2.0):
    print("Manual-op capture: handshake, then listen %ds while you operate the lock by hand."
          % seconds)
    print("  As you FINISH a manual op, type a label (e.g. 'lock'/'unlock') + Enter:")
    print("  -> a MARK line is stamped and we poll cmd07 once (the state the MCU believes).")
    print("  Frames we did not request are flagged *** MCU-INITIATED *** (event candidates).")
    print("  Note the LED too (ground truth): green blink = unlocked, amber = locked.")
    seq = _do_handshake(s, hello_timeout=hello_timeout, after=after)
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
                _check_expected(f)
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
                   "status cmd25 %s seq=%02X" % (_d(STEADY_REQ[1]), q))
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

class Tee:
    """Write to both the live console stream and a log file. The console stream
    keeps its own line-buffering, so output stays live; the file is flushed on
    every write so a second viewer sees it immediately."""
    def __init__(self, stream, fileobj):
        self.stream = stream
        self.fileobj = fileobj
    def write(self, data):
        self.stream.write(data)
        self.fileobj.write(data)
        self.fileobj.flush()
    def flush(self):
        self.stream.flush()
        self.fileobj.flush()
    def isatty(self):
        return self.stream.isatty()
    def fileno(self):
        return self.stream.fileno()

def _install_tee(path):
    """Tee stdout+stderr to `path` in addition to the console."""
    fobj = open(path, "w")
    sys.stdout = Tee(sys.__stdout__, fobj)
    sys.stderr = Tee(sys.__stderr__, fobj)

def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("calibrate").add_argument("files", nargs="+")
    sub.add_parser("verify").add_argument("files", nargs="+")
    parsers = {}
    for m in ("sniff","emulate","act","lock","unlock","manual"):
        p = sub.add_parser(m)
        p.add_argument("-p","--port",required=True)
        p.add_argument("--swap",action="store_true")
        p.add_argument("--seconds",type=int,default=(120 if m=="manual" else 30))
        p.add_argument("-v","--verbose",action="store_true",
                       help="log every frame: timestamp + CRC ok/BAD (+ raw unframed bytes)")
        p.add_argument("--after",choices=["hello","cmd09"],default="cmd09",
                       help="handshake sync point: 'cmd09' = after the full prefix [default]; "
                            "'hello' = after frame one")
        p.add_argument("--hello-timeout",type=int,default=15,
                       help="seconds to wait for the 913's boot prefix before giving up")
        p.add_argument("--log",nargs="?",const="auto",default=None,metavar="FILE",
                       help="tee all output to a log file: bare --log = logs/<mode>_TIMESTAMP.log, "
                            "--log FILE = explicit path")
        parsers[m] = p
    pemu = parsers["emulate"]
    pemu.add_argument("--poll",type=float,default=2.0,
                      help="seconds between periodic cmd25 status reports (0 = disable)")
    pact = parsers["act"]
    pact.add_argument("--settle",type=int,default=8,help="seconds of handshake before injecting")
    pact.add_argument("--mode",choices=["cluster","single"],default="cluster",
                      help="cluster = full observed actuation sequence; single = one cmd4E frame (A/B test)")
    pact.add_argument("--seq",choices=["track","capture","zero"],default="track",
                      help="track = next-available SEQ (fixes stale-seq); capture = capture-3 SEQs; zero = 0x00")
    pact.add_argument("--roles",choices=["as-captured","e7"],default="as-captured",
                      help="as-captured = the cluster's original role bytes; e7 = force every "
                           "frame to role=E7 (the direction we send) -- the direction A/B")
    pact.add_argument("--poll",type=float,default=2.0,
                      help="seconds between periodic cmd25 status reports during the test (0 = quiet baseline)")
    pact.add_argument("--frames",default=None,metavar="LIST",
                      help="cluster mode only: comma-separated ACT_CLUSTER indices to inject, "
                           "e.g. --frames 4 = cmd03 (LOCK) alone; --frames 7 = cmd05 (UNLOCK) alone")
    # Convenience: drive one absolute state command (bench-confirmed both
    # directions: cmd03 = LOCK, cmd05 = UNLOCK).
    for m in ("lock","unlock"):
        pm = parsers[m]
        pm.add_argument("--settle",type=int,default=8,help="seconds of handshake before injecting")
        pm.add_argument("--seq",choices=["track","capture","zero"],default="track",
                        help="track = next-available SEQ (fixes stale-seq); capture = capture-3 SEQs; zero = 0x00")
        pm.add_argument("--poll",type=float,default=2.0,
                        help="seconds between periodic cmd25 status reports during the test (0 = quiet baseline)")
    pman = parsers["manual"]
    pman.add_argument("--poll",type=float,default=2.0,
                      help="seconds between periodic cmd25 status reports (0 = fully quiet listen; "
                           "the session may idle out -- the hello retry loop would then be visible)")
    a = ap.parse_args()
    if getattr(a, "log", None):
        logpath = a.log
        if logpath == "auto":
            os.makedirs("logs", exist_ok=True)
            logpath = "logs/%s_%s.log" % (a.cmd, time.strftime("%Y%m%d_%H%M%S"))
        _install_tee(logpath)
        print("Logging to %s" % logpath)

    if a.cmd == "calibrate":
        frames = {}
        for fn in a.files:
            frames.update(reconstruct(parse_csv(fn)))
        calibrate(frames)
    elif a.cmd == "verify":
        frames = {}
        for fn in a.files:
            frames.update(reconstruct(parse_csv(fn)))
        for name, fs in frames.items():
            print(f"== {name} ==")
            for f in fs:
                if f: print("  " + frame_text(f))
    else:
        VERBOSE = a.verbose
        s = open_port(a.port, swap=a.swap)
        try:
            if a.cmd=="sniff": sniff(s, a.seconds)
            elif a.cmd=="emulate": emulate(s, seconds=a.seconds, hello_timeout=a.hello_timeout, after=a.after, poll_every=a.poll)
            elif a.cmd=="manual": manual(s, seconds=a.seconds, hello_timeout=a.hello_timeout, after=a.after, poll=a.poll)
            elif a.cmd=="act": act(s, seconds=a.seconds, settle=a.settle, mode=a.mode, seqmode=a.seq, hello_timeout=a.hello_timeout, after=a.after, poll=a.poll, roles=a.roles, frames=_parse_frames(a.frames))
            elif a.cmd in ("lock","unlock"):
                c = 0x03 if a.cmd == "lock" else 0x05
                st = (0x01, "LOCKED") if a.cmd == "lock" else (0x02, "unlocked")
                act(s, seconds=a.seconds, settle=a.settle, mode="cluster", seqmode=a.seq,
                    hello_timeout=a.hello_timeout, after=a.after, poll=a.poll, roles="e7",
                    frames=[_cluster_idx(c)], label="%s (cmd%02X)" % (a.cmd.upper(), c),
                    confirm_state=st)
        except KeyboardInterrupt:
            print("\nstopped")
        s.close()

if __name__ == "__main__":
    main()