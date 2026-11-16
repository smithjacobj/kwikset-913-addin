#!/usr/bin/env python3
"""Kwikset 913/914 <-> Z-Wave Module UART bench harness (emulates the module).

CORRECTED DIRECTION (Nov 2026 rebuild): the 913 INITIATES. It transmits the boot
hello (role AA, cmd18) and, left unanswered, re-sends a hello+cmd09 retry prefix
every ~2.6 s. We (the module) answer by transmitting role=E7 requests; the 913
replies to each (role AA/55), echoing our request SEQ. See the PROTOCOL MODEL
section below for the full corrected role model."""
import argparse, sys, struct, time

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
    0x07: (0xAA, [0x02]),
    0x08: (0xAA, [0x00]),
    0x09: (0xAA, [0x46]),
    0x30: (0x55, [0x03]),
    0x25: (0xAA, []),
}

def _d(data):
    return " ".join("%02X" % x for x in data) or "-"

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
    Silent on a match; one warning line if role or data differ from EXPECTED."""
    exp = EXPECTED.get(f["fields"][3])
    if exp is None:
        return
    e_role, e_data = exp
    role, data = f["fields"][2], f["fields"][4:]
    if role != e_role or data != e_data:
        print("      ^^ MISMATCH cmd%02X: want role=%02X data=%s, got role=%02X data=%s"
              % (f["fields"][3], e_role, _d(e_data), role, _d(data)))

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
        return None
    print("Prefix seen -> transmitting our E7 request sequence.")
    return send_reqs(s, 0x01)     # 913 echoes our SEQ in replies (match probe_e7.py)

# =====================================================================
# ACTUATION CLUSTER  (OPEN PROBLEM — live test pending)
# =====================================================================
# These are the actuation frames from the CSV capture. DIRECTION UNDER THE
# CORRECTED ROLE MODEL IS AMBIGUOUS and NOT yet validated:
#   * The old model read them as "module (us) TX". But the CSV actually places
#     the actuation frames on the MCU->module wire, so with corrected roles the
#     MCU likely INITIATES the actuation (pushes it to the module) — the module
#     may not command lock/unlock directly.
#   * The role bytes below (E7/55/AA mix) are as-captured; under the corrected
#     model the MCU only ever transmits AA/55, so a mixed-role "module TX"
#     cluster is internally inconsistent with the confirmed direction.
# Treat this as ONE testable variant for the live actuation test (next step):
# fire it after the corrected handshake and watch for a beep / 0x25 status flip.
# CHECK bytes are computed by build_frame(); SEQ is assigned at run time.
#
# Each entry: (role, cmd, data, capture_seq).
ACT_CLUSTER = [
    (0xE7, 0x09, [0x5A], 0x02),  # req  cmd09 data5A
    (0x55, 0x4E, [0x04], 0x0C),  # cmd  cmd4E data04  <- the trigger
    (0xAA, 0x07, [0x00], 0x0E),  # resp cmd07 data00
    (0x55, 0x2A, [0x14], 0x03),  # cmd  cmd2A data14
    (0xAA, 0x03, [],     0x0F),  # resp cmd03
    (0x55, 0x2A, [0x14], 0x04),  # cmd  cmd2A data14 (repeat)
    (0xAA, 0x03, [],     0x0F),  # resp cmd03 (repeat)
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

def _inject(s, mode, seqmode, base, roles="as-captured"):
    """Fire the actuation.
      mode 'single' = one cmd4E frame (the trigger, for A/B).
      mode 'cluster' = the full observed ACT_CLUSTER, per-frame SEQ per seqmode.
      roles 'as-captured' = the cluster's original role bytes; 'e7' = force every
        frame to role=E7 (the direction WE send) -- the direction A/B for the
        open actuation question."""
    if mode == "single":
        q = _next_seq(seqmode, base, 0, 0x0C)
        r = ROLE_MOD if roles == "e7" else 0x55
        tx(s, build_frame(q, r, 0x4E, [0x04]),
           "INJECT cmd4E data04 seq=%02X role=%02X" % (q, r))
        return
    for i, (role, cmd, data, capseq) in enumerate(ACT_CLUSTER):
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
        print("     Still check: did it BEEP? A beep with no new frame = an internal")
        print("     reaction (the MCU acted but sent no distinct frame).")
        print("     If no beep either, the as-captured roles are likely the wrong")
        print("     direction -- next A/B: re-run with --roles e7 (we send E7).")

def act(s, seconds=30, settle=8, mode="cluster", seqmode="track",
        hello_timeout=15.0, after="cmd09", poll=2.0, roles="as-captured"):
    print("Act (corrected direction): handshake, then inject %s (roles=%s) after "
          "%ds settle (seq=%s, poll=%ss). Ctrl-C to stop."
          % (mode, roles, settle, seqmode, poll))
    print("  Watch for: a BEEP + any *** NEW *** post-injection frame + any role=55")
    print("  async event. The harness prints a baseline->inject->watch summary.")
    print("  OPEN PROBLEM: actuation direction is unvalidated. On the bench the lock")
    print("  can't physically actuate, so a beep / new MCU frame is the observable")
    print("  signal. (A 0x25 status flip needs the assembled lock.)")
    seq = _do_handshake(s, hello_timeout=hello_timeout, after=after)
    if seq is None:
        return
    end = time.time() + seconds
    settle_at = time.time() + settle
    last_poll = 0.0
    injected = False
    max_seq = 0x00
    baseline = {}      # (role,cmd) -> count, seen BEFORE the injection
    post = {}          # (role,cmd) -> count, seen AFTER the injection
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
                    if injected and rc not in baseline:
                        print("    *** NEW post-injection frame: role=%02X cmd=%02X" % rc)
                    elif injected and rc[0] == ROLE_MCU_CMD:
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
                      % (mode, roles, seqmode, max_seq))
                _inject(s, mode, seqmode, max_seq, roles)
                print(">>> === POST-INJECTION WATCH: listen for a BEEP ===\n")
                injected = True
    except KeyboardInterrupt:
        pass
    _print_act_summary(baseline, post)

def sniff(s, seconds=30):
    print("Passive sniff for %ds (shows which pin carries which direction). Ctrl-C to stop." % seconds)
    print("(run with -v to also log raw/unframed bytes, timestamps, and CRC ok/BAD)")
    end = time.time() + seconds
    while time.time() < end:
        b = recv_frame(s, timeout=1.0)
        if b:
            log_frame("<-", b)

def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("calibrate").add_argument("files", nargs="+")
    sub.add_parser("verify").add_argument("files", nargs="+")
    parsers = {}
    for m in ("sniff","emulate","act"):
        p = sub.add_parser(m)
        p.add_argument("-p","--port",required=True)
        p.add_argument("--swap",action="store_true")
        p.add_argument("--seconds",type=int,default=30)
        p.add_argument("-v","--verbose",action="store_true",
                       help="log every frame: timestamp + CRC ok/BAD (+ raw unframed bytes)")
        p.add_argument("--after",choices=["hello","cmd09"],default="cmd09",
                       help="handshake sync point: 'cmd09' = after the full prefix [default]; "
                            "'hello' = after frame one")
        p.add_argument("--hello-timeout",type=int,default=15,
                       help="seconds to wait for the 913's boot prefix before giving up")
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
    a = ap.parse_args()

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
            elif a.cmd=="act": act(s, seconds=a.seconds, settle=a.settle, mode=a.mode, seqmode=a.seq, hello_timeout=a.hello_timeout, after=a.after, poll=a.poll, roles=a.roles)
        except KeyboardInterrupt:
            print("\nstopped")
        s.close()

if __name__ == "__main__":
    main()