# Kwikset 913/914 ↔ Z-Wave Module UART Protocol

Human-readable reference for the reverse-engineered protocol between the 913's
MCU and the Z-Wave add-in module (the 14-pin header on the 913 is **J4**; the
pin map is in `hardware/`, where J* designators on other sheets can be ignored).

**Status: bench-validated.** Every item below was confirmed on a live bench
board (ID blob `08 00 45 01`) and cross-checked against the old-revision
captures (`captures/*.csv`, board ID `04 00 36 0D`). Confidence re-validation:
**5/5 PASS** (Nov 18, fresh batteries). Companion harness: the `kwikset_bench`
package in `source/kwikset_bench/` (the top-level `kwikset_bench.py` is now a
thin shim over it — see §15).

Evidence classes used throughout:

- **bench-confirmed** — reproduced live on the new bench board.
- **capture** — seen in the old-revision captures (real Z-Wave module + MCU).
- **hypothesis** — not yet confirmed.

---

## 1. Physical layer & workflow

| Item | Value |
|---|---|
| UART | 9600 baud, 8N1 |
| Frame start | `0xBD` |
| Boot prefix | sent **only at power-on** |
| Run workflow | **start the script first, then power-cycle the 913** |
| Enable pin | must be driven High for the module line to be active |
| Port discipline | only one process may hold the port at a time |

## 2. Frame layout

Every frame on the wire, either direction:

| Offset | Name | Size | Meaning |
|---:|---|---:|---|
| 0 | `BD` | 1 | Frame start, always `0xBD` |
| 1 | `LEN` | 1 | `4 + len(DATA)` — counts SEQ, ROLE, CMD, DATA, CHECK |
| 2 | `SEQ` | 1 | Sequence number `0x01..0xFF`. MCU resets to `0x01` on each hello retry; MCU **echoes our SEQ** in replies |
| 3 | `ROLE` | 1 | Direction/type: `0xE7` / `0xAA` / `0x55` (see §4) |
| 4 | `CMD` | 1 | Command byte (see §7) |
| 5.. | `DATA` | n | Variable-length payload (`LEN − 4` bytes) |
| 5+n | `CHECK` | 1 | CRC-8 over `[LEN..DATA]` (see §3) |

Total on-wire length = `2 + LEN` bytes.

C-struct form:

```c
#include <stdint.h>

#define KWIKSET_BD        0xBD

/* Role byte: marks direction, not just type. */
enum {
    KWIKSET_ROLE_MOD     = 0xE7, /* module -> MCU : requests (the ONLY direction that actuates) */
    KWIKSET_ROLE_MCU_AX  = 0xAA, /* MCU -> module : replies to module requests (echoes our SEQ) */
    KWIKSET_ROLE_MCU_CMD = 0x55, /* MCU -> module : MCU-initiated commands / async events */
};

/* CRC-8: poly 0x80 (x^8+x^7), reflected (LSB-first), init 0xFF, xorout 0x00. */
#define KWIKSET_CRC_POLY  0x80
#define KWIKSET_CRC_INIT  0xFF

typedef struct __attribute__((packed)) {
    uint8_t  bd;        /* 0xBD frame start */
    uint8_t  len;       /* 4 + sizeof(data); counts seq..check */
    uint8_t  seq;       /* 0x01..0xFF; MCU resets to 0x01 per retry; echoed in replies */
    uint8_t  role;      /* KWIKSET_ROLE_MOD / _MCU_AX / _MCU_CMD */
    uint8_t  cmd;       /* command byte, see the command tables */
    uint8_t  data[0];   /* flexible array member: (len - 4) payload bytes */
    uint8_t  check;     /* final byte of the len block: CRC-8 over [len .. data] */
} kwikset_frame_t;
```

## 3. Check byte (CRC-8)

| Parameter | Value |
|---|---|
| Polynomial | `0x80` (`x^8 + x^7`) |
| Reflected | yes (LSB-first) |
| Init | `0xFF` |
| Xorout | `0x00` |
| Input range | `[LEN, SEQ, ROLE, CMD, DATA...]` — **not** the `BD` byte, **not** the check itself |

Worked example (the MCU boot hello):

```
frame      : BD 05 01 AA 18 00 49
crc input  : 05 01 AA 18 00
crc8(...)  = 0x49  == frame check  ✓
```

Validated 33/33 against all captured complete frames and continuously on the
live bench. The `kwikset_bench.py` `calibrate`/`verify` subcommands reproduce
this.

## 4. Role model (direction)

The role byte encodes **direction**. Settled by a live A/B: the MCU answers
our `0xE7` frames and ignores our `0xAA` frames.

| ROLE | Direction | Meaning |
|---:|---|---|
| `0xE7` | module → MCU | Requests — the only direction that actuates (we send these) |
| `0xAA` | MCU → module | Replies to our requests, echoing our SEQ |
| `0x55` | MCU → module | MCU-initiated commands / async events |

**Provenance note (Nov 19 re-reading):** the primer's "known-good sample
frames" had the two capture channel blocks **swapped**. By CSV channel names:
`TXD1` (from the MCU) carries the hello, AA replies, config block, cmd25 acks,
the cmd09-5A prompt, the 55 cmd2A motor-completes, and AA echo-acks; `RXD1`
(to the MCU) carries the module's entire E7 sequence and its actuation frames.
Both revisions therefore speak the **same** role model — the apparent
"direction flip" between revisions was the primer's mislabel, not a revision
difference. Caveat: this is a re-reading of channel labels; if the old capture
rig had crossed wiring, the conclusion flips.

## 5. Handshake (boot) flow

```
 user power-cycles the 913 (script already running)

 MCU  -> AA cmd18 00            hello
 MCU  -> AA cmd09 <boot byte>   retry prefix
        (the pair re-sends every ~2.6 s, SEQ reset to 0x01, until answered)

 WE   -> E7 cmd02  (readID)          SEQ 01
 WE   -> E7 cmd07  (status poll)     SEQ 02
 WE   -> E7 cmd08                   SEQ 03
 WE   -> E7 cmd09                   SEQ 04
 WE   -> E7 cmd35  (config)         SEQ 05   bench: no reply
 WE   -> E7 cmd30                   SEQ 06

 MCU  -> AA cmd02 08 00 45 01        ID blob          (echoes SEQ 01)
 MCU  -> AA cmd07 <state byte>                       (echoes SEQ 02)
 MCU  -> AA cmd08 00                                   (echoes SEQ 03)
 MCU  -> AA cmd09 46        (bench)                   (echoes SEQ 04)
 MCU  -> 55 cmd30 03        (bench)                   (echoes SEQ 06)

 steady state:
 WE   -> E7 cmd25 27 00        status report, every ~2 s
 MCU  -> AA cmd25 (empty)      ack (the varying byte is SEQ-dependent CHECK,
                               not a data byte)
```

- The MCU **initiates**; the module answers. Replies echo the request's SEQ.
- The in-dialogue cmd09 reply is `46` on the bench board. The MCU-initiated
  **boot** cmd09 byte varies: `3C` / `50` (unstable, low-battery boots),
  `46` (low batteries), `64` (fresh batteries) — undecoded, no actuation
  effect.
- `manual`/`act`/`lock`/`unlock` all start from this handshake (`--hello-timeout`
  covers slow/power-on boots; `--after hello` syncs on frame one only).

## 6. Absolute actuation model

**Bench-confirmed both directions:**

| Command | Meaning |
|---|---|
| `E7 cmd03` (no data) | **absolute LOCK** — extend bolt; motor runs to the end state even when already there (no-op reseat) |
| `E7 cmd05` (no data) | **absolute UNLOCK** — retract bolt; motor runs to the end state even when already there (no-op) |

Capture-derived details (real module's behavior, the gold reference):

- Per actuation event the module sends **BOTH** state commands, **target last**:
  from locked → `cmd03` (no-op reseat) then `cmd05`; from unlocked → `cmd05`
  (no-op) then `cmd03`.
- Status polls (`cmd07`) are interleaved, ~6.3 s apart.
- The MCU's async **motor-complete** arrives a fixed ~10.5 s after **every**
  state command (no-op included); the module waits for it before the next.

So: command the absolute end state you want; it is idempotent.

**What does NOT actuate** (bench A/B, all rules closed):

| Candidate | Result |
|---|---|
| `cmd4E` alone (and `cmd4E` in cluster) | acked, no movement — **ack-only** |
| `cmd2A 14` alone | acked, no movement — **ack-only** |
| `cmd09 5A + cmd07 00` (dialogue pair) | acked, no movement — converses, never executes |
| Full cluster **minus** `cmd4E`, all forced to E7 | **actuated** (dialogue frames led the burst) |
| `cmd03` alone (×2 samples) | **actuated**, often un-acked |
| As-captured (original 55/AA) roles | ignored entirely — the MCU only acts on E7 |

Rule of thumb from all 7 act tests: **every run that actuated contained
`cmd03`; every run that didn't lacked it.** The 55 ack is **not** a reliable
actuation indicator — physical observation + the `cmd07` readback are the
ground truth.

## 7. Command reference

### 7.1 Module → MCU (role `0xE7`) — we send; the only direction that actuates

| CMD | Name | DATA | Reply from MCU | Notes |
|---:|---|---|---|---|
| `02` | readID | — | `AA cmd02` + ID blob | `08 00 45 01` (bench) / `04 00 36 0D` (capture) |
| `03` | **absolute LOCK** | — | 55 motor-complete (ack often absent) | the trigger, confirmed alone ×2 |
| `05` | **absolute UNLOCK** | — | 55 motor-complete (ack often absent) | the trigger, confirmed alone |
| `07` | status poll | — | `AA cmd07` + 1 state byte | pure read, never actuates |
| `08` | (undecoded) | — | `AA cmd08 00` | handshake step |
| `09` | (undecoded) | — | `AA cmd09 46` (bench) / `5A` (capture) | see §5 |
| `12` | dipswitch settings query (GET) | — | `AA cmd12` + bitmask byte (§7.3) | Z-Wave param 31 pass-through; GET-only |
| `25` | module status report | `27 00` (steady state) | `AA cmd25` (empty) | periodic; capture also saw `29 00`, `2A 00` — undecoded |
| `30` | (undecoded) | — | `55 cmd30 03` (bench) / `AA 01` (capture) | handshake step |
| `35` | config | — | `16 × 0x20` (capture) / **none** (bench) | handshake step |
| `4E` | prompt answer | `04` (capture) | `55 cmd4E 04` (capture) | answers the MCU's ~12 s `cmd09 5A` prompt; ack-only |

Not currently emulated (gold reference does, open question): `cmd02` **×3 at
boot**, and the ~12 s `cmd4E` answer to the `cmd09 5A` prompt.

### 7.2 MCU → module (role `0xAA` / `0x55`)

| ROLE | CMD | Name | DATA | Notes |
|---:|---:|---|---|---|
| `AA` | `18` | hello | `00` | boot sync point |
| `AA` | `02` | ID reply | `08 00 45 01` (bench) | board fingerprint (§9) |
| `AA` | `07` | state reply | `01` / `02` (bench); `00` (capture) | state readout (§8) |
| `AA` | `08` | reply | `00` | |
| `AA` | `09` | reply | `46` (bench) / `5A` (capture) | capture's `5A` is a **prompt** |
| `AA` | `12` | dipswitch settings reply | 1 bitmask byte (§7.3) | Z-Wave param 31 pass-through; GET-only |
| `AA` | `25` | status ack | (empty) | ack of our `cmd25` |
| `AA` | `30` | reply | `01` (capture) | bench replies `55 03` |
| `AA` | `03` / `05` | echo-ack | — | ~60 ms after motor-complete (capture); flaky on bench (2/8) |
| `AA` | `4E` | ack | `04` (capture) | ack of our `cmd4E` prompt answer |
| `55` | `2A` | **motor-complete** | `15 FF` (bench) / `14` (capture) | async; ~3–4 s after actuation (bench), ~10.5 s (capture, incl. no-ops) |
| `55` | `30` | reply | `03` (bench) | |

### 7.3 Dipswitch settings (`cmd12` reply byte)

The `AA cmd12` reply carries one **bitmask** byte. Cross-referenced against
Kwikset's own Z-Wave documentation (SmartCode 914 manual, cert ZC10-18106261):
this is **Configuration parameter 31 "Dipswitch settings"** — read-only,
factory default `5`. The Z-Wave JS UI exposes it as **GET-only** (no SET), so
the UART `cmd12` is that same GET pass-through: the module queries (no data),
the MCU answers with the current dipswitch image. A SET makes no sense at any
level: the value is the position of a **physical** switch, changeable only by
hand — a hardware-state readout, not a stored setting. (The user's controller also
shows class 112, Manufacturer Specific; Kwikset's docs place param 31 under
Configuration, 113 — the bit semantics match either way.) The dip switches are
on the **lock's rear panel**, not on the module.

| Bit | Value | Setting (per Kwikset Z-Wave docs) |
|---:|---:|---|
| 0 | `0x01` | Lock Status LED |
| 1 | `0x02` | Autolock |
| 2 | `0x04` | Buzzer |
| 3 | `0x08` | Secure Screen / handing invert (model-dependent) |

**Evidence** (capture class; `captures/lock-status-led-dip-switch.csv`, Nov 19
22:15 UTC): the real module sent two **mid-session** `E7 cmd12` GETs (no data;
SEQ `0D`/`0E`) — triggered by a **parameter refresh from the Z-Wave JS UI**
(controller-driven, not module-scheduled) — and the MCU answered `AA cmd12`
with data `00` then `01` (12–18 ms, SEQ echoed, zero `cmd25` in the window).
The `00` → `01` transition matches the user toggling the Lock Status LED
switch off → on. **Bench-confirmed** (Nov 19 16:00, `poll --settings`): the
bench board answers `cmd12` with the same bitmask (~15 ms); two mid-run
dipswitch flips tracked as `01 → 03 → 07` (LED; +Autolock; +Buzzer). That
same run then surfaced the post-autolock UART deafness (§14.12).

## 8. State readout (`cmd07` reply data byte)

| Byte | Meaning |
|---:|---|
| `01` | **LOCKED** (bench board) |
| `02` | **unlocked** (bench board) |
| `00` | **unknown** — no conclusive closed-loop state. Observed in low-battery/unstable runs (motor stalled short of full travel → no end-stop → no conclusive state; the same condition can also be **silence**: the MCU skipping replies entirely) |

State **survives a power-cycle** (bolt position + MCU belief both persist).

## 9. Board differences (old capture board vs new bench board)

| Item | Capture board (`04 00 36 0D`) | Bench board (`08 00 45 01`) |
|---|---|---|
| cmd02 ID blob | `04 00 36 0D` | `08 00 45 01` |
| cmd07 readout | `00` | `01` locked / `02` unlocked |
| in-dialogue cmd09 reply | `5A` (→ ~12.06 s prompt → `cmd4E` answer → `55 cmd4E 04`) | `46` (prompt never observed) |
| boot cmd09 byte | — | varies `3C`/`50`/`46`/`64` (undecoded) |
| cmd35 reply | `16 × 0x20` | none |
| cmd30 reply | `AA 01` | `55 03` |
| motor-complete | `55 cmd2A 14`, ~10.5 s (fixed, incl. no-ops) | `55 cmd2A 15 FF`, ~3–4 s (absent in all 8 afternoon runs → confirm falls back to a fixed wait and still passes) |
| echo-ack after complete | ~60 ms | flaky (2/8) |
| low-battery behavior | — | `00` readout, skipped replies, or no boot prefix at all |
| real Z-Wave module pairing | worked | **barebones** (§11) |

## 10. Manual (key) operation

- **Capture (Nov 18, two runs): the MCU is completely silent on manual ops** —
  zero MCU-initiated frames; state appears only in response to **our** `cmd07`
  requests.
- **Bench (Nov 19, reproduced Nov 20): an internally-triggered action
  (autolock, §14.12) is equally frameless** — the MCU broadcast no
  motor-complete for it. It sends no motor events for any
  non-module-initiated action (key *or* its own logic); state changes
  surface only via our `cmd07` polls. Catch: the Nov 20 autolock went deaf
  (§14.12) within ~15 s of locking, so the `cmd07` polls never saw that
  state change at all.
- **Add-in detection design:** periodic `cmd07` polling + diffing. Treat `00`
  as *"unknown — no state change"* and **never** as a new state (a weak-battery
  stall must not register as a phantom toggle).
- The harness `manual` subcommand implements this (stdin MARK lines + post-mark
  `cmd07` poll); the typed-mark path has not yet been exercised live.

## 11. LED & status indicators

- **Ground truth:** status LED blinks **green ~5–7 s** when unlocked, **amber
  (green+red)** when locked; bolt position survives power-cycle.
- **Bench artifact (deferred, user instruction: don't troubleshoot now):** the
  status LED stays **off** after bench UART commands; it returns to blinking
  only after **physical (key)** manipulation. Re-confirmed on the healthy unit.

## 12. Z-Wave barebones data point

A real old-revision Z-Wave module paired with the **new** board goes
**barebones**: excluded/re-included, the interview exposes only basic
config/info classes, **no lock classes** — the module appears to reject the
unknown board (top suspect: the `cmd02` ID-blob fingerprint). This is
**one-directional**: the 913 board has no rejection logic (it has happily
accepted our emulated module throughout) → **no effect on the DIY add-in.**

## 13. Timing constants

| Constant | Value | Context |
|---|---|---|
| hello retry period | ~2.6 s | MCU re-sends hello+cmd09, SEQ reset to `01` |
| steady-state poll | ~2 s | our `cmd25` status report interval (configurable) |
| motor-complete (bench) | ~3–4 s | after any state command |
| motor-complete (capture) | ~10.5 s | fixed, including no-ops |
| capture cmd07 poll spacing | ~6.3 s | real module's interleaved polls |
| CONFIRM_SETTLE | 1.0 s | settle after motor-complete before reading state |
| CONFIRM_FALLBACK | 10.0 s | fixed wait if the (flaky) motor-complete never arrives |

## 14. Open questions

1. `cmd2A` data `14` vs `15 FF` — what do the two bytes encode?
2. `cmd4E` data `04` — semantics.
3. `cmd25` data variants `27` / `29` / `2A 00` — what differs?
4. ID blob (`04 00 36 0D` / `08 00 45 01`) — fingerprint vs revision vs
   capability bits (the real module's barebones rejection makes this a
   first-class question for future real-module compatibility).
5. Boot cmd09 byte variants `3C`/`50`/`46`/`64` — battery state vs boot mode?
6. Does the MCU skip handshake replies (incl. in-dialogue cmd09) because of
   something in our request timing/content?
7. `cmd35` no reply on bench; `cmd30` answered with role `55` on bench —
   revision behavior or our emulated module's influence?
8. LED-off artifact (deferred by user instruction).
9. Do the un-emulated gold-reference extras matter: boot-time `cmd02 ×3` and
   the ~12 s `cmd4E` answer? Cheap test: run `emulate`, watch for a ~12 s
   prompt on the new board.
10. Old-capture-rig caveat: if its wiring was crossed, the channel re-reading
    (§4 provenance) flips.
11. `cmd25` activity gating: in the cmd12 capture the real module sent **zero**
    `cmd25` reports across a 17 s mid-session window — is the module's
    `cmd25` activity gated on nearby activity rather than a fixed ~2 s
    cadence? A longer post-boot capture is planned to check whether the
    activity quiesces.
12. **Post-autolock UART deafness** (bench; first observed Nov 19 16:00
    `poll --settings` run, **reproduced Nov 20** — refined below).
    Nov 19: ~23–30 s after the Autolock switch was turned on, the MCU stopped
    answering **everything** — no `cmd07`/`cmd12` replies, no `cmd25` acks,
    no 55 motor-complete for the autolock, no boot-prefix re-send; ≥80 s of
    silence. It was alive at the last instant before (it batched out two
    stale `cmd25` acks just before going quiet), and every one of our frames
    stayed CRC-clean — polite silence, not a link-layer desync.
    Nov 20 (two episodes: a fresh autolock — bolt move observed 09:37:30.685,
    replies die within ~15 s — and 17.5 h of silence since the Nov 19
    episode):
    - The deaf MCU is **not fully deaf**: poked WITHOUT a handshake (`poke`)
      it answers exactly `AA cmd08 00` + `AA cmd09 64` and nothing else
      (`cmd02`/`cmd07`/`cmd12`/`cmd25`/`cmd30`/`cmd35` ignored) — the
      identical 2-frame pair in both episodes.
    - A poke (our full E7 sequence + `cmd07` + `cmd12`) does **NOT** restore
      the full command set, and 17.5 h of silence did not recover it either.
    - **Only observed recovery: a power cycle** (Nov 20 09:36:52 run: prefix,
      full session, state `01`, settings `03`). The 09:36:55 prefix was the
      user's power cycle (confirmed) — the prefix is sent **at power-on
      only**; no spontaneous re-send.
    - This RESOLVES the Nov 19 adapter-vs-MCU question: the bench adapter
      delivers (the frames were received) and the MCU is alive (it sent
      them) ⇒ MCU-side **selective command processing**. What that state
      actually is (reduced ISR? sleep mode? watchdog?) is still open, and
      whether a key op recovers a deaf MCU is untested.
    - Add-in implications: (a) the `cmd07`-diff detection model has a hole
      for internally-triggered state changes that coincide with deafness
      (the autolock's own LOCK is invisible to it — the Nov 20 lock was
      first seen via the bolt, not the UART); (b) the add-in is powered
      from the lock (cannot power-cycle it) → detect the
      cmd08/cmd09-only signature on a poke and **flag the condition**
      rather than acting on silence.

## 15. Harness cross-reference (`kwikset_bench` package)

Package layout (all under `source/`):

| File | Contents |
|---|---|
| `kwikset_bench.py` | 13-line shim — the original `kwikset_bench.py <mode> ...` invocation keeps working unchanged |
| `kwikset_bench/__main__.py` | `python -m kwikset_bench` entry point (same CLI) |
| `kwikset_bench/cli.py` | argparse wiring for every subcommand (`lock`/`unlock` map onto `act()`) |
| `kwikset_bench/crc.py` | CRC-8 implementation (§3: poly `0x80` reflected, init `0xFF`, xorout `0x00`) + table builder |
| `kwikset_bench/frames.py` | `build_frame`/`split_frame`/`frame_text`, CSV capture parsing + reconstruction (the `calibrate`/`verify` data path) |
| `kwikset_bench/protocol.py` | ALL protocol constants (roles, `REQS`, `EXPECTED`, `STEADY_REQ`, `ACT_CLUSTER`, `STATE_WORDS`, `SETTINGS_MASK`, boot prefix) + decoders (`fmt_data`, `state_word`, `decode_settings`), each with its provenance comment |
| `kwikset_bench/link.py` | transport: `VERBOSE`, timestamps, CRC validation, `log_frame`/`tx`/`recv_frame`, `--log` Tee, `open_port` |
| `kwikset_bench/handshake.py` | `send_reqs`/`check_expected`/`poke_round`/`wait_prefix`/`do_handshake` |
| `kwikset_bench/modes/sniff.py` | passive listen |
| `kwikset_bench/modes/emulate.py` | full module steady state (handshake + periodic `cmd25`) |
| `kwikset_bench/modes/act.py` | `inject()` + frame-spec parsing, `read_state`/`read_settings`, `act()` (the target `lock`/`unlock` map onto) |
| `kwikset_bench/modes/manual.py` | capture what the MCU transmits on hand/key operation |
| `kwikset_bench/modes/poll.py` | state (+ optionally `--settings`) polling, keepalive, `--with-poke` |
| `kwikset_bench/modes/poke.py` | deaf-state triage probe (§14.12) |
| `kwikset_bench/modes/calibrate.py` | CRC model calibration / verification |

The old monolith's private names were de-underscored where they became the
public API (e.g. `_confirm_state` → `read_state`, `_wait_prefix` →
`wait_prefix`, `_do_handshake` → `do_handshake`, `_install_tee` →
`install_tee`); behavior is unchanged.

Subcommand behavior:

| Subcommand | Purpose |
|---|---|
| `sniff` | passive listen (pin/direction check) |
| `emulate` | full module steady state (handshake + periodic `cmd25`) |
| `act` | injection tests (`--mode`, `--frames`, `--roles e7`, `--seq`, `--poll`) |
| `lock` / `unlock` | drive one absolute state command + state readback confirm (PASS/FAIL) |
| `manual` | capture what the MCU transmits on hand/key operation |
| `poll` | handshake, then read state (`cmd07`) every `--interval` s and flag changes; `--keepalive` (`cmd25`) keeps the session alive; `--settings` also reads the dipswitch settings (`cmd12`, §7.3) each cycle; `--with-poke` makes the prefix wait active (below) |
| `poke` | NO handshake: N rounds of our E7 request sequence + `cmd07` + `cmd12` with a listen window between rounds — the deaf-state triage probe (bench 11-20: a deaf MCU answers `cmd08`/`cmd09` only, §14.12) |
| `calibrate` / `verify` | CRC model against captures |

Flags: `-p PORT`, `-v` (timestamps + CRC ok/BAD + raw), `--after hello|cmd09`,
`--hello-timeout N`, `--with-poke` (all handshake modes: poke every 3 s while
waiting for the prefix — for a lock that was NOT power-cycled; a cmd07 answer
or a power-on prefix both start the session; on timeout the replies seen
distinguish "fully silent" from the deaf state), `--log [FILE]` (tee to
`logs/<mode>_TIMESTAMP.log`).
Typical confidence run:

```
../.venv/bin/python kwikset_bench.py lock -p /dev/tty.usbserial-AB0LPKC3 \
    -v --hello-timeout 120 --log
```

Deaf-lock triage (diagnosis needs no power cycle; recovery is the power
cycle — §14.12):

```
../.venv/bin/python kwikset_bench.py poke -p /dev/tty.usbserial-AB0LPKC3 \
    -v --log
../.venv/bin/python kwikset_bench.py poll -p /dev/tty.usbserial-AB0LPKC3 \
    -v --with-poke --settings --log
```

**Run workflow reminder:** start the script first, then power-cycle the 913
(the boot prefix is sent only at power-on).

Tests: `source/tests/` holds the plain-assert, no-hardware suites (no
pytest) that drive the real mode functions against fake MCUs; `tests/` is
a namespace package, so from `source/` either works:
`../.venv/bin/python -m tests.test_poll` or
`../.venv/bin/python tests/test_poll.py` (see `tests/README.md`).

## 16. Known harness gaps

- `recv_frame` has no `SerialException` tolerance (a dropped USB serial
  connection raises instead of logging).
- `ACT_CLUSTER` still transcribes the capture's MCU-side frames (provenance in
  `kwikset_bench/protocol.py`); `lock`/`unlock` use `--frames <idx>` to inject the
  genuine E7 trigger, so the cluster is only a bisection/`act` convenience now.
- Stdin MARK path in `manual` never exercised live (optional verification run
  is on the table).