"""poll: handshake, then read the lock state (cmd07) every N seconds.

=====================================================================
STATE POLLING  (handshake, then read state every N seconds)
=====================================================================
The add-in's manual-op detection model in running form: handshake, then
periodically poll cmd07 (a pure read -- it never actuates) and read the
state byte back, flagging changes. Per the add-in design: 00 / no-reply =
"unknown -- no state change"; it is NEVER treated as a new state (a
weak-battery stall must not register as a phantom toggle). A periodic
cmd25 keepalive keeps the session alive (the MCU can idle out; see
manual --poll). Ctrl-C to stop.

--settings additionally reads the dipswitch settings each cycle (cmd12 GET,
no data -- the UART form of Z-Wave parameter 31 "Dipswitch settings",
read-only, factory default 5) and flags changes the same way. See
PROTOCOL.md 7.3.
"""
import time

from .act import read_settings, read_state
from ..frames import build_frame
from ..handshake import check_expected, do_handshake
from ..link import log_frame, recv_frame, tx
from ..protocol import (ROLE_MOD, SETTINGS_MASK, STATE_WORDS, STEADY_REQ,
                        decode_settings, fmt_data, state_word)

def poll(s, seconds=300, interval=5.0, keepalive=2.0, settings=False,
         hello_timeout=15.0, after="hello", poke_interval=0.0):
    print("State poll: handshake, then read state (cmd07) every %.1fs for %ds "
          "(cmd25 keepalive every %.1fs). Ctrl-C to stop." % (interval, seconds, keepalive))
    print("  Readout: 01=LOCKED 02=unlocked 00=unknown. Unknown/no-reply is treated")
    print("  as 'no state change' -- never as a new state.")
    if settings:
        print("  --settings: each cycle also reads the dipswitch settings (cmd12 = Z-Wave")
        print("  param 31, read-only) and flags changes: 01=Lock Status LED 02=Autolock")
        print("  04=Buzzer 08=Secure Screen (model-dependent); factory default 5.")
    seq = do_handshake(s, hello_timeout=hello_timeout, after=after,
                       poke_interval=poke_interval)
    if seq is None:
        return
    # Drain the handshake replies still queued (ID blob, the boot-time state
    # readout, ...) so the first timed poll is a fresh cmd07 we sent.
    print("Draining handshake replies...")
    while True:
        b = recv_frame(s, timeout=1.0)
        if not b:
            break
        f = log_frame("<-", b)
        if f:
            check_expected(f)
    t0 = time.time()
    end = t0 + seconds
    next_poll = next_keep = t0
    last_state = None
    last_settings = None
    polls = changes = schanges = 0
    try:
        while time.time() < end:
            now = time.time()
            if now >= next_poll:
                next_poll = now + interval
                polls += 1
                state, seq = read_state(s, seq)
                if state is None:
                    print("  [t=%6.1fs] no cmd07 reply -> unknown (no state change)"
                          % (now - t0))
                elif state in STATE_WORDS:
                    word = state_word(state)
                    if last_state is None:
                        print("  [t=%6.1fs] state: %s  (initial readout)" % (now - t0, word))
                    elif state != last_state:
                        changes += 1
                        print("  [t=%6.1fs] *** STATE CHANGE: %s -> %s ***"
                              % (now - t0, state_word(last_state), word))
                    else:
                        print("  [t=%6.1fs] state: %s  (unchanged)" % (now - t0, word))
                    last_state = state
                else:
                    print("  [t=%6.1fs] state byte %s -> unknown (no state change)"
                          % (now - t0, fmt_data([state])))
                if settings:
                    sbyte, seq = read_settings(s, seq)
                    if sbyte is None:
                        print("  [t=%6.1fs] settings: no cmd12 reply -> unknown (no change)"
                              % (now - t0))
                    else:
                        dec = decode_settings(sbyte & SETTINGS_MASK)
                        if last_settings is None:
                            print("  [t=%6.1fs] settings: %02X = %s  (initial readout)"
                                  % (now - t0, sbyte, dec))
                        elif sbyte != last_settings:
                            schanges += 1
                            print("  [t=%6.1fs] *** SETTINGS CHANGE: %02X (%s) -> %02X (%s) ***"
                                  % (now - t0, last_settings,
                                     decode_settings(last_settings & SETTINGS_MASK),
                                     sbyte, dec))
                        else:
                            print("  [t=%6.1fs] settings: %02X = %s  (unchanged)"
                                  % (now - t0, sbyte, dec))
                        last_settings = sbyte
            if keepalive and now >= next_keep:
                next_keep = now + keepalive
                tx(s, build_frame(seq, ROLE_MOD, STEADY_REQ[0], STEADY_REQ[1]),
                   "status cmd25 %s seq=%02X (keepalive)" % (fmt_data(STEADY_REQ[1]), seq))
                seq = (seq + 1) & 0xFF
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nstopped")
    print("\n=== POLL SUMMARY ===")
    print("  polls: %d   state changes: %d   final state: %s"
          % (polls, changes,
             state_word(last_state) if last_state is not None else "never read"))
    if settings:
        print("  settings changes: %d   final settings: %s"
              % (schanges,
                 ("%02X = %s" % (last_settings, decode_settings(last_settings & SETTINGS_MASK)))
                 if last_settings is not None else "never read"))
