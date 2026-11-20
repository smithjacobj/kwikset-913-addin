"""sniff: passive listen (shows which pin carries which direction)."""
import time

from ..link import log_frame, recv_frame

def sniff(s, seconds=30):
    print("Passive sniff for %ds (shows which pin carries which direction). Ctrl-C to stop." % seconds)
    print("(run with -v to also log raw/unframed bytes, timestamps, and CRC ok/BAD)")
    end = time.time() + seconds
    while time.time() < end:
        b = recv_frame(s, timeout=1.0)
        if b:
            log_frame("<-", b)
