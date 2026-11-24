"""emulate: module steady state (handshake, then log replies + periodic cmd25)."""
import time

from ..frames import build_frame
from ..handshake import check_expected, do_handshake
from ..link import log_frame, recv_frame, tx
from ..protocol import ROLE_MOD, STEADY_REQ, fmt_data

def emulate(s, seconds=30, hello_timeout=15.0, after="hello", poll_every=2.0,
            poke_interval=0.0):
    print("Emulate (corrected direction): wait for the 913's prefix, send our E7 "
          "request sequence, then log its replies and send periodic cmd25 status "
          "reports for %ds. Ctrl-C to stop." % seconds)
    seq = do_handshake(s, hello_timeout=hello_timeout, after=after,
                       poke_interval=poke_interval)
    if seq is None:
        return
    end = time.time() + seconds
    last_poll = 0.0
    while time.time() < end:
        b = recv_frame(s, timeout=1.0)
        if b:
            f = log_frame("<-", b)
            if f:
                check_expected(f)
        if poll_every and time.time() - last_poll >= poll_every:
            rcmd, rd = STEADY_REQ
            tx(s, build_frame(seq, ROLE_MOD, rcmd, rd),
               "status cmd25 %s seq=%02X" % (fmt_data(rd), seq))
            seq = (seq + 1) & 0xFF
            last_poll = time.time()