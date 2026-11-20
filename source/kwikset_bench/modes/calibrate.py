"""calibrate/verify: fit a CRC model against capture frames, and verify them.

calibrate tries candidate CRC-8 models x ranges against every frame and
reports the best match (the confirmed model is in crc.py; this brute-force
search is what found it). verify re-frames the captures and prints every
parsed frame (CRC-checked on display via frame_text's check byte).
"""
from ..crc import crc8
from ..frames import frame_text

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

def verify(frames):
    """Print every re-framed capture frame (per CSV channel)."""
    for name, fs in frames.items():
        print(f"== {name} ==")
        for f in fs:
            if f: print("  " + frame_text(f))
