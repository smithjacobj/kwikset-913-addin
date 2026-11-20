#!/usr/bin/env python3
"""End-to-end test of manual() against a fake MCU (no hardware).

Uses tests.fakes.McuFake with the simulated manual-op event enabled
(event_at=EVENT_AT): boots with the AA cmd18 + AA cmd09 prefix (retries every
2.6 s), answers our handshake reqs bench-style, acks cmd25 polls, and at
t=EVENT_AT emits the manual-op event (55 cmd2A 15 FF + AA cmd03) that
manual() must flag *** MCU-INITIATED ***.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes import McuFake
from kwikset_bench.modes.manual import manual

EVENT_AT = 6.0

print("########## manual() end-to-end vs fake MCU ##########")
fake = McuFake(flip_at=None, silent=None, event_at=EVENT_AT)
manual(fake, seconds=14, hello_timeout=15, after="cmd09", poll=2.0)