"""Live UART link + frame logging.

The VERBOSE global lives here: set it via the module attribute
(`link.VERBOSE = ...`) so every reader sees the update (a from-import
of the value would copy it once and go stale).
"""
import sys
import time

from .crc import CRC_POLY, CRC_INIT, CRC_XOR, crc8
from .frames import frame_text, split_frame

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

def install_tee(path):
    """Tee stdout+stderr to `path` in addition to the console."""
    fobj = open(path, "w")
    sys.stdout = Tee(sys.__stdout__, fobj)
    sys.stderr = Tee(sys.__stderr__, fobj)
