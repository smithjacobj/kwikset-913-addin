"""Protocol model (corrected direction, rebuild Nov 2026) + decoders.

ALL protocol constants live here: the role model, the MCU hello, our request
sequence, expected replies for THIS board, the cmd07 state words, the cmd12
dipswitch-settings bits, the actuation cluster, and the post-command confirm
timing. Provenance comments travel with the data.
"""

# =====================================================================
# PROTOCOL MODEL — CORRECTED DIRECTION  (rebuild, Nov 2026)
# =====================================================================
# The primer's role model had EVERY direction flipped. Settled on this board
# by a live A/B (probe_e7.py): the 913 answers our role=E7 frames and ignores
# our role=AA frames. So the role byte marks direction, not just type:
#
#   ROLE  0xE7   module -> MCU   (requests)        <- WE transmit these
#   ROLE  0xAA   MCU -> module   (responses)       <- the 913 transmits these
#   ROLE  0x55   MCU -> module   (cmd / async)     <- the 913 transmits these
#
# Handshake (corrected): the MCU INITIATES. It transmits the hello (AA cmd18)
# and, left unanswered, re-sends a hello+cmd09 retry prefix every ~2.6 s (SEQ
# resets to 01 each retry). The module (us) answers by transmitting its request
# sequence (E7); the 913 replies to each request (AA/55), echoing our request's
# SEQ. Steady state: we periodically send cmd25 (E7, a status report); the 913
# acks it (AA, empty data — the varying byte is the SEQ-dependent CHECK, not a
# data byte).
#
# NOTE: this board is a DIFFERENT REVISION than start-unlock-lock.csv — same
# command set + SEQ-echo, but board-specific payloads (see EXPECTED below). The
# CSV's old hardcoded values (id 04 00 36 0D, cmd09 5A, cmd30 01/AA, cmd35
# 16x20) do NOT match this board and are gone.
# 19th re-reading of the captures (by CSV channel names): the primer's
# "known-good sample frames" had the two channel blocks SWAPPED. TXD1 (from
# the MCU) carries the hello, the AA replies, the config block, the cmd25
# acks, the cmd09 5A prompt, the 55 cmd2A motor-completes, and the AA
# echo-acks; RXD1 (to the MCU) carries the module's entire E7 request
# sequence (cmd02 x3, cmd07/08/09, cmd35, cmd30, cmd25 reports 27/29/2A 00,
# the cmd4E answer) and its actuation (E7 cmd03/cmd05, both, target last,
# cmd07 polls interleaved). So the two revisions speak the SAME role model:
# the "direction flip" was the primer's mislabel, not a revision difference
# (plausibility: the motor-complete sits on the hello's channel, i.e. the
# side with the motor). Caveat: a re-reading of the channel labels -- if the
# old capture rig had crossed wiring, this flips.
# =====================================================================
ROLE_MOD    = 0xE7   # module -> MCU : requests (what WE send)
ROLE_MCU_AX = 0xAA   # MCU -> module : responses to our requests
ROLE_MCU_CMD= 0x55   # MCU -> module : initiated commands / async events

# The MCU's boot hello — our sync point. (role, cmd, data)
MCU_HELLO = (0xAA, 0x18, [0x00])

# Our request sequence (all role=E7), sent once at handshake. (cmd, data)
# Gold reference (the capture's RXD1 = the module's real behavior): it sent
# cmd02 THREE times at boot, then cmd07/08/09, cmd35, cmd30, and ~12 s in it
# answered the MCU's cmd09 5A prompt with a cmd4E. Neither the boot-time
# cmd02 repeat nor the cmd4E answer is emulated here (open: do they matter?).
REQS = [
    (0x02, []),               # readID
    (0x07, []),
    (0x08, []),
    (0x09, []),
    (0x35, []),               # config — this board: no reply
    (0x30, []),
]
# Steady-state poll, sent periodically. (cmd, data) — a module status report.
STEADY_REQ = (0x25, [0x27, 0x00])

# Expected 913 replies for THIS board, keyed by our request cmd: (role, data).
# Validation/logging only — the 913 sends these, we don't. (No 0x35 entry: this
# board does not reply to the config request.)
EXPECTED = {
    0x02: (0xAA, [0x08, 0x00, 0x45, 0x01]),  # ID/cap blob (board-specific; the
                            # capture board was 04 00 36 0D). 19th: the real
                            # old-revision Z-Wave module paired with THIS board
                            # goes BAREBONES -- excluded/re-included, the
                            # interview exposes only basic config/info classes,
                            # no lock classes (it appears to reject the unknown
                            # board; ID-blob fingerprint is the top suspect).
                            # No effect on our DIY add-in (no such rejection
                            # logic) -- data point.
    # 0x07: state-dependent -- handled in check_expected (01 locked /
    # 02 unlocked on this bench board; the capture board replies 00).
    0x08: (0xAA, [0x00]),
    0x09: (0xAA, [0x46]),  # in-dialogue reply. The MCU-initiated BOOT prefix
                            # cmd09 byte varies instead: 3C (18th 10:37) and 50
                            # (18th 11:29, unstable/low-battery boots), 46 (18th
                            # 14:13, low batteries), 64 (18th 14:52-15:02, fresh
                            # batteries). Undecoded; no effect on actuation.
    0x30: (0x55, [0x03]),
    0x25: (0xAA, []),
}

def fmt_data(data):
    return " ".join("%02X" % x for x in data) or "-"

# cmd07 reply data byte -> human state (bench board: 01 locked / 02 unlocked;
# the capture board replies 00). Shared by check_expected and confirm_state.
# 00 "unknown" = no conclusive closed-loop state: observed on the bench board
# in the 11-18 10:35/36 unstable/low-battery runs, where the motor apparently
# stalled short of full travel (user's hypothesis: no end-stop -> no
# conclusive state). The 18th 14:14 INCONCLUSIVE (MCU skipped all 3 confirm
# replies) is likely the same condition expressed as silence. Never seen with
# fresh batteries (5/5 clean readouts the 18th afternoon).
STATE_WORDS = {0x01: "LOCKED", 0x02: "unlocked"}
def state_word(b):
    return STATE_WORDS.get(b, "unknown")

# cmd12 reply data byte -> dipswitch settings bitmask. Cross-reference
# (Kwikset's own Z-Wave docs, SmartCode 914 manual): Configuration parameter
# 31 "Dipswitch settings", READ-ONLY, factory default 5. The Z-Wave JS UI
# exposes it as GET-only; the UART cmd12 is that same GET pass-through
# (module queries with no data, MCU answers with the current dipswitch
# image). The dip switches are on the lock's rear panel, not on the module.
# See PROTOCOL.md 7.3.
SETTINGS_BITS = {0x01: "Lock Status LED",
                  0x02: "Autolock",
                  0x04: "Buzzer",
                  0x08: "Secure Screen (model-dependent)"}
SETTINGS_MASK = 0x0F
def decode_settings(b):
    names = [v for k, v in SETTINGS_BITS.items() if b & k]
    return ", ".join(names) if names else "(all known bits off)"

# =====================================================================
# ACTUATION CLUSTER
# =====================================================================
# Actuation frames transcribed from the CSV captures. PROVENANCE (19th
# re-reading, see the PROTOCOL MODEL note): the as-captured roles below are
# the capture's TXD1 channel = the MCU's OWN frames (the prompt, the acks,
# the motor-completes, the state-cmd echo-acks) -- replaying them as-is is
# the 16th negative A/B (the MCU ignores non-requests). The module's genuine
# actuation frames (the RXD1 E7 sequence: cmd03/cmd05, both, target last,
# cmd07 polls interleaved) were never transcribed here; bisection "found"
# cmd03 because the MCU's AA echo of it is byte-identical to the module's
# E7 cmd03 once roles are corrected.
#
# LIVE-VALIDATED 2026-11-16/17 (bench 913, each test starting UNLOCKED):
#   * --roles as-captured (original 55/AA bytes): the MCU ignored everything
#     (negative). It only acts on the E7 (module->MCU) direction.
#   * Full cluster as E7 (16th 15:34): lock ACTUATED; MCU sent 55 cmd4E
#     data03 (the cmd4E ack, not the motor event).
#   * cmd4E ALONE (16th 15:48): MCU acked but the motor did NOT move
#     -> cmd4E is ack-only, NOT the motor trigger.
#   * Cluster MINUS cmd4E (16th 16:03, frames 0,2,3,4,5,6): ACTUATED, no 55
#     frames (silent execution — the dialogue frames led the burst).
#   * cmd09 5A + cmd07 00 alone (16th 16:10, frames 0,2): the MCU replied
#     AA to both but the lock did NOT move -> dialogue pair ruled out; it
#     only converses, never executes.
#   * cmd2A 14 + cmd03, DUPLICATED (16th 16:39, frames 3,4,5,6): ACTUATED;
#     MCU replied 55 cmd2A data03 + 55 cmd03 (motor frames led the burst).
#   * cmd2A 14 + cmd03, SINGLE COPY (17th 10:43, frames 3,4): ACTUATED
#     -> duplicates are NOT required; the single ordered pair is the minimal
#     known trigger. Wire: 55 cmd2A data03 (+32 ms), then a delayed 55 cmd2A
#     data=15 FF (~2.9 s later, seq=03) — likely motor-complete; 1 sample,
#     undecoded. The 55 cmd03 reply appeared only with the duplicated pair.
#   * cmd2A 14 ALONE (17th 11:02, frame 3): MCU acked 55 cmd2A data03 but the
#     lock did NOT move -> cmd2A is ack-only (like cmd4E), NOT the trigger.
#   * cmd03 ALONE (17th 11:08 AND 11:21, frame 4, 2 samples): the lock MOVED
#     both times, yet the MCU did NOT ack it (no 55, no AA cmd03 — only the
#     periodic cmd25 acks). CONFIRMED reproducible. -> cmd03 is the motor
#     trigger, NOT cmd2A.
#   * CORRELATION (all 7 act tests): every run that ACTUATED contained cmd03;
#     every run that did NOT (cmd09+cmd07, cmd2A alone) lacked it. cmd03
#     presence is the differentiator — the 55 ack is NOT a reliable actuation
#     indicator (cmd4E/cmd2A are acked but inert; cmd03 actuates, often
#     un-acked). Physical observation is the ground truth.
#   * Open (lock semantics): cmd03 is ZERO-DATA, so the trigger carries no
#     payload -- does it LOCK, UNLOCK, or TOGGLE? All 7 tests started manually
#     UNLOCKED and drove to LOCKED. Next experiment: a LOCKED-start run (user
#     locks, then power-cycles with the script running). ASSUMPTION to verify:
#     does the MCU's internal bolt state (or a boot position-sense) survive a
#     power-cycle? If not, the MCU may believe "unlocked" even when the bolt is
#     in, confounding the toggle-vs-absolute read of the result.
#   * Open (data bytes, now lower priority): what cmd2A data14 / cmd4E data04 /
#     cmd09 data5A encode; reply data03 appears on BOTH the cmd4E and cmd2A
#     acks -> a generic ack/status code, not a state value.
#
# CAPTURE-DERIVED MODEL (captures/start-*.csv: real Z-Wave module + this MCU,
# board ID 04 00 36 0D; same sniffer session as start-unlock-lock.csv):
#   * 12.06 s after the handshake the MCU spontaneously sends E7 cmd09 data5A
#     (a prompt); the module replies cmd4E; the MCU acks 55 cmd4E data04.
#   * Per actuation event the module sends BOTH state commands, target LAST:
#     from locked: cmd03 (no-op reseat), cmd05 (unlock); from unlocked:
#     cmd05 (no-op), cmd03 (lock) -- interleaved with cmd07 status polls
#     (~6.3 s apart). The MCU's async 55 cmd2A data14 = "motor complete,"
#     arriving a fixed ~10.5 s after EVERY state command (no-op included),
#     followed ~60 ms later by an AA echo-ack of the LAST state command.
#     The module waits for the complete event before issuing the next command.
#   => cmd03 = absolute LOCK (extend bolt), cmd05 = absolute UNLOCK (retract);
#      each motor-runs to the commanded end state even when already there.
#      Matches all 7 bench tests (unlocked start + cmd03 -> locked).
# BENCH-CONFIRMED BOTH DIRECTIONS (17th):
#   * cmd05 alone from LOCKED (13:37): bolt RETRACTED (unlocked). MCU sent
#     55 cmd2A data=15 FF ~3.9 s after injection (bench motor-complete);
#     no AA echo-ack this time (bench acks stay flaky).
#   * cmd03 alone from LOCKED (11:47): NO-MOVE no-op reseat -- confirmed by
#     the 13:37 handshake state readout (cmd07 reply 01 = still LOCKED at
#     the next boot).
#   * BENCH motor-complete = 55 cmd2A data 15 FF (~3-4 s after actuation;
#     2 samples: 10:43 pair run, 13:37 cmd05) vs the capture board's
#     55 cmd2A data 14 at ~10.5 s. It was absent in ALL 8 afternoon 18th
#     runs -- the confirm then runs via the CONFIRM_FALLBACK path and still
#     PASSES. Status LED stays OFF after our bench commands (real module
#     keeps it blinking); re-confirmed 18th on the healthy unit -- it
#     returns to blinking only after PHYSICAL (key) manipulation. Noted,
#     still deferred.
#   * CONFIDENCE RE-VALIDATION (18th afternoon, fresh batteries, better-
#     assembled unit): 5/5 PASS -- 14:52 unlock reseat (02), 14:53 lock
#     (02->01), 14:54 unlock (01->02), 14:54 lock (02->01), 15:02 lock
#     (02->01). Every confirm reply on attempt 1/3; every handshake readout
#     consistent with the previous run's end state (state survives a
#     power-cycle). The 10:35/10:36 FAILs (readback 02, then 00 "unknown")
#     were low-battery/unstable-state artifacts: the motor apparently stalled
#     short of full travel, so the closed-loop readout had no conclusive
#     state (00 "unknown", or silence; see the STATE_WORDS note). Same-day
#     low-battery runs: 14:13 PASS (no-op reseat), 14:14 INCONCLUSIVE (MCU
#     skipped all 3 confirm replies -- likely the same condition, expressed
#     as silence), 14:50 total silence (no boot prefix at all).
#   => cmd07 = status poll; its reply data byte is the state readout on the
#      bench board (01 locked / 02 unlocked; the capture board replies 00).
#
# CHECK bytes are computed by build_frame(); SEQ is assigned at run time.
#
# Each entry: (role, cmd, data, capture_seq).
ACT_CLUSTER = [
    (0xE7, 0x09, [0x5A], 0x02),  # req  cmd09 data5A   (dialogue only; no actuation)
    (0x55, 0x4E, [0x04], 0x0C),  # cmd  cmd4E data04   (module reply to the MCU's cmd09 5A prompt; ack-only)
    (0xAA, 0x07, [0x00], 0x0E),  # resp cmd07 data00   (status poll; reply byte = state readout)
    (0x55, 0x2A, [0x14], 0x03),  # cmd  cmd2A data14   (MCU "motor complete" async event, ~10.5s after a state cmd)
    (0xAA, 0x03, [],     0x0F),  # resp cmd03          (THE trigger: confirmed alone, x2, un-acked)
    (0x55, 0x2A, [0x14], 0x04),  # cmd  cmd2A data14 (repeat; not required)
    (0xAA, 0x03, [],     0x0F),  # resp cmd03 (repeat; not required)
    (0xE7, 0x05, [],     0x0F),  # req  cmd05 no data  (UNLOCK: bench-confirmed 17th 13:37 from locked)
]

# Post-command state confirmation (confidence reps). After a lock/unlock command
# the motor settles when the MCU emits its async motor-complete (55 cmd2A); we
# then poll cmd07 and read the state byte back, comparing it to the state we
# commanded. Timing: prefer the motor-complete + a short settle; if the (flaky)
# complete never arrives, fall back to a fixed post-injection wait.
CONFIRM_SETTLE   = 1.0    # s after the motor-complete before reading state
CONFIRM_FALLBACK = 10.0   # s after injection if no complete is seen (bench motor
                           # completes ~3-4 s; the capture board's ~10.5 s is the
                           # conservative reference)