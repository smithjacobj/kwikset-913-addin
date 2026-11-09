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

# ---------- live UART ----------
def open_port(path, baud=9600, swap=False):
    import serial
    s = serial.Serial(port=path, baudrate=baud, bytesize=8, parity='N',
                      stopbits=serial.STOPBITS_ONE, timeout=0.2)
    time.sleep(1.2)   # let DTR settle / MCU boot
    s.flushInput(); s.flushOutput()
    return s

# Frames to SEND as the module during emulate (from captured module TX, direction-agnostic).
# Checks are now computed by build_frame() against the confirmed CRC model.
# Seq values are carried from the capture (boot handshake is byte-identical every power-on).
HELO = build_frame(0x01, 0xAA, 0x18, [0x00])  # hello: seq01 roleAA cmd18 data00 -> check 0x49
# responses keyed by (role,cmd) we observed the module answering:
RESPS = {
    (0xE7, 0x02): build_frame(0x03, 0xAA, 0x02, [0x04, 0x00, 0x36, 0x0D]),  # id/cap blob -> 63
    (0xE7, 0x07): build_frame(0x04, 0xAA, 0x07, [0x00]),                    # -> 53
    (0xE7, 0x08): build_frame(0x05, 0xAA, 0x08, [0x00]),                    # -> 5D
    (0xE7, 0x09): build_frame(0x06, 0xAA, 0x25, []),                        # 25 status -> 72
    (0xE7, 0x35): build_frame(0x07, 0xAA, 0x35, [0x20]*16),                 # config 16x20 -> 73
    (0xE7, 0x30): build_frame(0x08, 0xAA, 0x30, [0x01]),                    # -> 69
}

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
    s.write(HELO); print("  -> sent hello 18")
    end = time.time() + seconds
    while time.time() < end:
        b = recv_frame(s, timeout=1.5)
        if not b:
            continue
        f = split_frame(b)
        if not f:
            continue
        print("  <- " + frame_text(f))
        key = (f["fields"][2], f["fields"][3])
        resp = RESPS.get(key)
        if resp:
            s.write(resp)
            print("  -> answered " + frame_text(split_frame(list(resp))))

def act(s, cmd=bytes([0x4E, 0x04]), seconds=30, settle=8, seq=0x0C):
    print("Handshake then inject command once MCU settles into poll loop.")
    s.write(HELO); print("  -> sent hello 18")
    end = time.time() + seconds
    injected = False
    settle_at = time.time() + settle
    while time.time() < end:
        b = recv_frame(s, timeout=1.5)
        if b:
            f = split_frame(b)
            if f:
                print("  <- " + frame_text(f))
                key = (f["fields"][2], f["fields"][3])
                if key in RESPS:
                    s.write(RESPS[key])
                    print("  -> answered")
        if not injected and time.time() >= settle_at:
            # CMD=cmd[0], DATA=cmd[1:], role 0x55 (async command/event). Correct check via build_frame.
            # NOTE: seq is the remaining open question (primer gotcha #7). Default 0x0C reproduces the
            # captured actuation frame (BD 05 0C 55 4E 04 E9); bump it if the MCU rejects on seq.
            frame = build_frame(seq, 0x55, cmd[0], cmd[1:])
            s.write(frame)
            print("  >> INJECTED cmd=%02X data=%s seq=%02X" % (cmd[0], ' '.join('%02X'%x for x in cmd[1:]) or "-", seq))
            injected = True

def sniff(s, seconds=30):
    print("Passive sniff for %ds (shows which pin carries which direction). Ctrl-C to stop." % seconds)
    end = time.time() + seconds
    while time.time() < end:
        b = recv_frame(s, timeout=1.0)
        if b:
            f = split_frame(b)
            print(("  <- " if f is None else "  <- ") + (frame_text(f) if f else ' '.join('%02X'%x for x in b)))

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("calibrate").add_argument("files", nargs="+")
    sub.add_parser("verify").add_argument("files", nargs="+")
    for m in ("sniff","emulate","act"):
        p = sub.add_parser(m); p.add_argument("-p","--port",required=True)
        p.add_argument("--swap",action="store_true")
        p.add_argument("--seconds",type=int,default=30)
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
        s = open_port(a.port, swap=a.swap)
        try:
            if a.cmd=="sniff": sniff(s, a.seconds)
            elif a.cmd=="emulate": emulate(s, a.seconds)
            elif a.cmd=="act": act(s, seconds=a.seconds)
        except KeyboardInterrupt:
            print("\nstopped")
        s.close()

if __name__ == "__main__":
    main()
