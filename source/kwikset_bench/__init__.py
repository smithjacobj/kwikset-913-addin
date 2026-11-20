"""Kwikset 913/914 <-> Z-Wave Module UART bench harness (emulates the module).

CORRECTED DIRECTION (Nov 2026 rebuild): the 913 INITIATES. It transmits the boot
hello (role AA, cmd18) and, left unanswered, re-sends a hello+cmd09 retry prefix
every ~2.6 s. We (the module) answer by transmitting role=E7 requests; the 913
replies to each (role AA/55), echoing our request SEQ. See kwikset_bench.protocol
for the full corrected role model, and source/PROTOCOL.md for the canonical
protocol reference.

Subcommands: sniff (passive listen), emulate (module steady state), act
(injection test), lock/unlock (drive one absolute state), manual (capture what
the MCU transmits when the lock is operated BY HAND -- the key path the Z-Wave
add-in board will also have to react to), poll (handshake, then read the lock
state via cmd07 every N seconds -- the add-in's detection model in running
form; --settings also reads the dipswitch settings bitmask via cmd12, the
UART form of Z-Wave parameter 31; --with-poke pokes while waiting for the
prefix, for a lock that was NOT power-cycled), poke (no handshake:
repeatedly send our E7 request sequence + cmd07 + cmd12 and listen for ANY
reply -- the wake probe for a possibly-deaf MCU; bench 11-20: a deaf MCU
answers cmd08/cmd09 only), calibrate/verify (CRC vs captures).

Package layout (see PROTOCOL.md 15): crc (CRC-8 primitives), frames
(build/parse/split + CSV), protocol (all constants + decoders), link (UART +
logging + Tee), handshake (prefix wait, request sequence, validation, poke
rounds), modes/ (one module per subcommand), cli (argparse + dispatch).
Run as `python -m kwikset_bench <mode> ...` or via the kwikset_bench.py shim.
"""
from .cli import main

__all__ = ["main"]