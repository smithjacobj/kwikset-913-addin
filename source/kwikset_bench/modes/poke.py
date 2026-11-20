"""poke: wake probe -- no handshake, what does a (possibly deaf) MCU answer?

=====================================================================
POKE / WAKE PROBE  (no handshake: what does a deaf MCU still answer?)
=====================================================================
For the post-autolock deafness (PROTOCOL.md 14.12). Bench 11-20 (two
episodes -- a fresh autolock, and 17.5 h of silence): the deaf MCU is
NOT fully deaf. Poked WITHOUT a handshake it answers exactly two
frames, `AA cmd08 00` + `AA cmd09 64`, and ignores cmd02/cmd07/cmd12/
cmd25/cmd30/cmd35. A poke round (our full E7 sequence + cmd07 + cmd12)
does NOT restore the full command set, and 17.5 h of silence did not
recover it either; the only recovery observed is a power cycle (the
boot prefix is sent at power-on only). So poke TRIAGES: a
cmd08/cmd09-only reply = the deaf state (power cycle to recover); a
full cmd07/cmd12 answer = merely idle, not deaf; total silence = a
fully silent MCU or a dead bench adapter RX (a power-cycle run
discriminates: prefix appears -> MCU recovered, adapter fine).
"""
import time

from ..handshake import check_expected, poke_round
from ..link import log_frame, recv_frame

def poke(s, rounds=10, interval=3.0, listen=2.0):
    print("Poke (no handshake): %d rounds of our E7 request sequence + cmd07 + "
          "cmd12, %.1fs apart, listening %.1fs each round for ANY reply. "
          "Ctrl-C to stop." % (rounds, interval, listen))
    t0 = time.time()
    seq = 0x01
    woke = False
    saw = {}
    try:
        for r in range(1, rounds + 1):
            seq = poke_round(s, seq, "POKE r%02d" % r)
            start = time.time()
            got = 0
            while time.time() - start < listen:
                b = recv_frame(s, timeout=0.5)
                if not b:
                    continue
                f = log_frame("<-", b)
                if f:
                    got += 1
                    c = f["fields"][3]
                    saw[c] = saw.get(c, 0) + 1
                    check_expected(f)
            if got and not woke:
                woke = True
                print("  >>> WAKE: first reply in round %d (t=%.1fs): %d frame(s)"
                      % (r, time.time() - t0, got))
                break
            if r < rounds:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped")
    if woke:
        got = ", ".join("cmd%02X x%d" % (c, n) for c, n in sorted(saw.items()))
        print("\n=== POKE SUMMARY ===")
        if set(saw) <= {0x08, 0x09}:
            print("  DEAF STATE: the MCU answered %s -- the post-autolock 'deaf'"
                  % got)
            print("  signature (PROTOCOL.md 14.12): it answers cmd08/cmd09 only,")
            print("  and a poke does NOT restore the full command set.")
            print("  Power-cycle the 913 to recover -- or keep probing with")
            print("  `poll --with-poke`: it pokes every 3s and will start the session")
            print("  if a full answer (or a power-on prefix) arrives; on timeout it")
            print("  re-prints this triage.")
        else:
            print("  WOKEN: the MCU answered %s -- the full command set, i.e." % got)
            print("  merely idle, not deaf. Re-run a normal handshake (lock/unlock/")
            print("  poll) for a healthy session.")
    else:
        print("\n=== POKE SUMMARY ===")
        print("  no reply in any round: the MCU (or the bench adapter's RX) is")
        print("  fully silent. Power-cycle the 913 and re-run: prefix seen ->")
        print("  MCU recovered (asleep/crashed, needs a power cycle); still")
        print("  silent -> check the adapter/wiring.")
