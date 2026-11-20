"""CRC-8 primitives for the 913/914 <-> Z-Wave module UART protocol."""

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