#!/usr/bin/env python3
"""Kwikset 913/914 <-> Z-Wave Module UART bench harness (emulates the module)."""
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

# Frames to SEND as the module during emulate (from captured module TX, direction-agnostic).
# Checks are computed by build_frame() against the confirmed CRC model.
HELO = build_frame(0x01, 0xAA, 0x18, [0x00])  # hello: seq01 roleAA cmd18 data00 -> check 0x49

# Response TEMPLATES, keyed by the (role,cmd) of the incoming MCU request we are
# answering. Each value is (reply_role, reply_cmd, reply_data). The SEQ is NOT
# stored here — it is assigned dynamically at send time by make_reply(), so
# repeated polls get distinct, fresh SEQs instead of one frozen boot-era value.
# (reply role is 0xAA — module response — for all of these.)
RESPS = {
    (0xE7, 0x02): (0xAA, 0x02, [0x04, 0x00, 0x36, 0x0D]),  # id/cap blob
    (0xE7, 0x07): (0xAA, 0x07, [0x00]),
    (0xE7, 0x08): (0xAA, 0x08, [0x00]),
    (0xE7, 0x09): (0xAA, 0x25, []),   # NOTE: replies to a cmd09 request with a cmd25
                                      # frame. Kept exactly as-captured — but primer boot
                                      # step 5 says the reply should be cmd09 data5A, and
                                      # the steady-state cmd25 poll is NOT covered at all
                                      # (no (0xE7,0x25) key). See make_reply / TODO below.
    (0xE7, 0x35): (0xAA, 0x35, [0x20]*16),                # config 16x20
    (0xE7, 0x30): (0xAA, 0x30, [0x01]),
}

def make_reply(req, seq=None):
    """Build the reply frame for an incoming request frame `req` (a split dict),
    or None if we have no response for it.

    SEQ is DYNAMIC: by default it ECHOES the request's own SEQ (reply_seq ==
    req_seq). This matches the capture — the steady-state 0x25 polls arrive at
    seq 09/0A/0B... and the module's status replies mirror them at 09/0A/0B... —
    and it eliminates the old frozen boot-era SEQ that made repeated replies
    duplicates. Pass seq= to override (e.g. a fixed value for an A/B test).
    """
    r_role, r_cmd = req["fields"][2], req["fields"][3]     # (role,cmd) of the request
    tpl = RESPS.get((r_role, r_cmd))
    if tpl is None:
        return None
    reply_role, reply_cmd, reply_data = tpl
    if seq is None:
        seq = req["fields"][1]                             # echo the request SEQ
    return build_frame(seq, reply_role, reply_cmd, reply_data)

# Actuation cluster — the frames the MODULE (us) sends to trigger lock/unlock.
# Reconstructed from capture 3's "Module TX (we send)" actuation frames
# (primer known-good lines 107-113). Each entry: (role, cmd, data, capture_seq).
# CHECK bytes are computed by build_frame() against the confirmed CRC model.
#
# CAUTION — ORDER, ROLES and SEQ are HYPOTHESES, not ground truth:
#   * Role semantics (E7/AA/55) are a working model, not proven (primer Q4).
#   * Lock vs unlock appear to use the SAME frames (primer Q2); a real
#     assembled-lock capture is still needed to see the 0x25 status flip.
#   * The capture SEQs (4th field) are non-monotonic — they're what the module
#     happened to send in that one trace, so treat them as ONE testable variant,
#     not a rule. Use --seq to switch the assignment strategy at run time.
# Interleaved with these, the MCU sends cmd4E/05/07/03/07 (role E7) — see the
# primer's "MCU RX (we receive)" actuation frames.
ACT_CLUSTER = [
    (0xE7, 0x09, [0x5A], 0x02),  # req  cmd09 data5A   (capture check 4C)
    (0x55, 0x4E, [0x04], 0x0C),  # cmd  cmd4E data04   (capture check E9) <- the trigger
    (0xAA, 0x07, [0x00], 0x0E),  # resp cmd07 data00   (capture check 59)
    (0x55, 0x2A, [0x14], 0x03),  # cmd  cmd2A data14   (capture check 92)
    (0xAA, 0x03, [],     0x0F),  # resp cmd03         (capture check 5D)
    (0x55, 0x2A, [0x14], 0x04),  # cmd  cmd2A data14 (repeat) (capture check 95)
    (0xAA, 0x03, [],     0x0F),  # resp cmd03         (repeat) (capture check 5D)
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

def emulate(s, seconds=30):
    print("As the Z-Wave Module: sending hello, then answering MCU for %ds. Ctrl-C to stop." % seconds)
    tx(s, HELO, "hello")
    end = time.time() + seconds
    while time.time() < end:
        b = recv_frame(s, timeout=1.5)
        if not b:
            continue
        f = log_frame("<-", b)          # verbose-aware log; returns split dict or None
        if not f:
            continue
        resp = make_reply(f)            # dynamic-SEQ reply (echoes the request SEQ)
        if resp:
            tx(s, resp, "answered %02X" % f["fields"][3])

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

def _inject(s, mode, seqmode, base):
    """Fire the actuation. mode 'single' = one cmd4E frame (old behavior, for A/B);
    'cluster' = the full observed ACT_CLUSTER with per-frame SEQ per seqmode."""
    if mode == "single":
        q = _next_seq(seqmode, base, 0, 0x0C)
        tx(s, build_frame(q, 0x55, 0x4E, [0x04]), "INJECT cmd4E data04 seq=%02X" % q)
        return
    for i, (role, cmd, data, capseq) in enumerate(ACT_CLUSTER):
        q = _next_seq(seqmode, base, i, capseq)
        d = ' '.join('%02X' % x for x in data) or "-"
        tx(s, build_frame(q, role, cmd, data), "INJECT %02X %s seq=%02X" % (cmd, d, q))

def act(s, seconds=30, settle=8, mode="cluster", seqmode="track"):
    print("Handshake, then inject %s after %ds settle (seq=%s). Ctrl-C to stop." % (mode, settle, seqmode))
    tx(s, HELO, "hello")
    end = time.time() + seconds
    settle_at = time.time() + settle
    injected = False
    max_seq = 0x00
    while time.time() < end:
        b = recv_frame(s, timeout=1.5)
        if b:
            f = log_frame("<-", b)          # verbose-aware log; returns split dict or None
            if f:
                max_seq = max(max_seq, f["fields"][1])     # fields[1] = SEQ
                resp = make_reply(f)                       # dynamic-SEQ reply (echo)
                if resp:
                    tx(s, resp, "answered %02X" % f["fields"][3])
        if not injected and time.time() >= settle_at:
            _inject(s, mode, seqmode, max_seq)
            injected = True

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
        parsers[m] = p
    pact = parsers["act"]
    pact.add_argument("--settle",type=int,default=8,help="seconds of handshake before injecting")
    pact.add_argument("--mode",choices=["cluster","single"],default="cluster",
                      help="cluster = full observed actuation sequence; single = one cmd4E frame (A/B test)")
    pact.add_argument("--seq",choices=["track","capture","zero"],default="track",
                      help="track = next-available SEQ (fixes stale-seq); capture = capture-3 SEQs; zero = 0x00")
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
            elif a.cmd=="emulate": emulate(s, a.seconds)
            elif a.cmd=="act": act(s, seconds=a.seconds, settle=a.settle, mode=a.mode, seqmode=a.seq)
        except KeyboardInterrupt:
            print("\nstopped")
        s.close()

if __name__ == "__main__":
    main()
