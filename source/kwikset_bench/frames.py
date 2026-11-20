"""Wire frames: build/parse/split, plus CSV capture reconstruction and display.

Frame layout: [BD][LEN][SEQ][ROLE][CMD][DATA...][CHECK]. LEN counts
SEQ,ROLE,CMD,DATA,CHECK (4 + data length); the CRC runs over
[LEN,SEQ,ROLE,CMD,DATA...] (see crc.CRC_*).
"""
from .crc import CRC_POLY, CRC_INIT, CRC_XOR, crc8

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

def frame_text(f):
    fl = f["fields"]
    # fl = [len, seq, role, cmd, *data]
    ln, seq, role, cmd = fl[0], fl[1], fl[2], fl[3]
    data = fl[4:]
    return (f"len={ln:2d} seq={seq:02X} role={role:02X} cmd={cmd:02X} "
            f"data={' '.join('%02X'%b for b in data):<12} check={f['check']:02X}")