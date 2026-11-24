"""act: handshake, then inject the actuation cluster (or a subset) and watch.

The lock/unlock subcommands are convenience wrappers around act() in the CLI
(a single ACT_CLUSTER frame + a confirm_state readback), not separate modules.
"""
import time

from ..frames import build_frame
from ..handshake import check_expected, do_handshake
from ..link import log_frame, recv_frame, tx
from ..protocol import (ACT_CLUSTER, CONFIRM_FALLBACK, CONFIRM_SETTLE,
                        ROLE_MCU_CMD, ROLE_MOD, STEADY_REQ, fmt_data, state_word)

def next_seq(seqmode, base, i, capseq):
    """Assign the SEQ for cluster frame i.
      'track'   -> base+1+i : next-available after the highest SEQ seen from the MCU.
                       This is the fix for the stale 0x0C injection (primer gotcha #7).
      'capture' -> capseq    : exact SEQ from capture 3, to reproduce it byte-for-byte.
      'zero'    -> 0x00      : probe whether the MCU checks SEQ at all.
    """
    if seqmode == "capture":
        return capseq
    if seqmode == "zero":
        return 0x00
    return (base + 1 + i) & 0xFF

def cluster_idx(cmd, data=b""):
    """First ACT_CLUSTER index whose (cmd, data) match (for lock/unlock)."""
    for i, (role, c, d, capseq) in enumerate(ACT_CLUSTER):
        if c == cmd and d == list(data):
            return i
    raise SystemExit("no ACT_CLUSTER entry for cmd%02X data=%s" % (cmd, fmt_data(data)))

def parse_frames_spec(spec):
    """Parse a --frames spec like '0,2,3,4,5,6' into cluster indices."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        i = int(part)
        if not 0 <= i < len(ACT_CLUSTER):
            raise SystemExit("--frames index %d out of range 0..%d"
                             % (i, len(ACT_CLUSTER) - 1))
        out.append(i)
    return out or None

def inject(s, mode, seqmode, base, roles="as-captured", frames=None):
    """Fire the actuation.
      mode 'single' = one cmd4E frame (isolation A/B).
      mode 'cluster' = the observed ACT_CLUSTER, or the --frames subset of it,
        per-frame SEQ per seqmode.
      roles 'as-captured' = the cluster's original role bytes; 'e7' = force every
        frame to role=E7 (the direction WE send)."""
    if mode == "single":
        q = next_seq(seqmode, base, 0, 0x0C)
        r = ROLE_MOD if roles == "e7" else 0x55
        tx(s, build_frame(q, r, 0x4E, [0x04]),
           "INJECT cmd4E data04 seq=%02X role=%02X" % (q, r))
        return
    idxs = frames if frames is not None else range(len(ACT_CLUSTER))
    for i in idxs:
        role, cmd, data, capseq = ACT_CLUSTER[i]
        if roles == "e7":
            role = ROLE_MOD
        q = next_seq(seqmode, base, i, capseq)
        d = ' '.join('%02X' % x for x in data) or "-"
        tx(s, build_frame(q, role, cmd, data),
           "INJECT %02X %s seq=%02X role=%02X" % (cmd, d, q, role))

def print_act_summary(baseline, post):
    """End-of-run report: which (role,cmd) the MCU sent before vs after the
    injection, and whether anything NEW appeared after it."""
    new = sorted(set(post) - set(baseline))
    print("\n=== ACT SUMMARY ===")
    print("  pre-injection baseline (role,cmd):count")
    for rc, n in sorted(baseline.items()):
        print("    role=%02X cmd=%02X  x%-3d" % (rc[0], rc[1], n))
    print("  post-injection (role,cmd):count")
    for rc, n in sorted(post.items()):
        tag = "   *** NEW" if rc not in baseline else ""
        print("    role=%02X cmd=%02X  x%-3d%s" % (rc[0], rc[1], n, tag))
    if new:
        print("  -> NEW frame(s) after injection: %s"
              % ", ".join("role=%02X/cmd=%02X" % rc for rc in new))
        print("     The MCU reacted to the injection (it never sent these before).")
    else:
        print("  -> no NEW frame after injection; only repeats of baseline frames.")
        print("     Still check: did it BEEP or did the lock move? A motor action")
        print("     with no new frame = an internal reaction (the MCU acted but")
        print("     sent no distinct frame). Note: the validated trigger direction")
        print("     is --roles e7 (module->MCU); as-captured 55/AA are ignored.")

# Post-command state confirmation (confidence reps). After a lock/unlock command
# the motor settles when the MCU emits its async motor-complete (55 cmd2A); we
# then poll cmd07 and read the state byte back, comparing it to the state we
# commanded. Timing: prefer the motor-complete + a short settle; if the (flaky)
# complete never arrives, fall back to a fixed post-injection wait.
# (Named read_state -- NOT confirm_state: act() takes a confirm_state
# parameter and would shadow a same-named module-level function.)
def read_state(s, seq, retries=3, per_retry=2.0):
    """Send a cmd07 status poll and read the state byte back, retrying if the MCU
    skips the reply (known flaky, 3 samples). Returns (state_byte or None,
    next_seq). cmd07 is a read (role E7) and never actuates."""
    for attempt in range(1, retries + 1):
        q = seq & 0xFF
        seq = (seq + 1) & 0xFF
        tx(s, build_frame(q, ROLE_MOD, 0x07, []),
           "CONFIRM cmd07 seq=%02X (attempt %d/%d)" % (q, attempt, retries))
        start = time.time()
        while time.time() - start < per_retry:
            b = recv_frame(s, timeout=0.5)
            if not b:
                continue
            f = log_frame("<-", b)
            if not f:
                continue
            if f["fields"][2] == 0xAA and f["fields"][3] == 0x07:
                data = f["fields"][4:]
                if data:
                    return data[0], seq
                # no data byte yet; keep waiting this attempt
    return None, seq

def read_settings(s, seq, retries=2, per_retry=1.0):
    """Send a cmd12 dipswitch-settings GET (no data) and read the reply byte
    back, retrying if the MCU skips the reply. Returns (byte or None,
    next_seq). Read-only: the reply is the settings bitmask, never an
    actuation (PROTOCOL.md 7.3)."""
    for attempt in range(1, retries + 1):
        q = seq & 0xFF
        seq = (seq + 1) & 0xFF
        tx(s, build_frame(q, ROLE_MOD, 0x12, []),
           "SETTINGS cmd12 seq=%02X (attempt %d/%d)" % (q, attempt, retries))
        start = time.time()
        while time.time() - start < per_retry:
            b = recv_frame(s, timeout=0.5)
            if not b:
                continue
            f = log_frame("<-", b)
            if not f:
                continue
            if f["fields"][2] == 0xAA and f["fields"][3] == 0x12:
                data = f["fields"][4:]
                if data:
                    return data[0], seq
                # no data byte yet; keep waiting this attempt
    return None, seq

def act(s, seconds=30, settle=8, mode="cluster", seqmode="track",
        hello_timeout=15.0, after="hello", poll=2.0, roles="as-captured",
        frames=None, label=None, confirm_state=None, poke_interval=0.0):
    what = label if label is not None else (mode if frames is None else "subset %s" % frames)
    print("Act (corrected direction): handshake, then inject %s (roles=%s) after "
          "%ds settle (seq=%s, poll=%ss). Ctrl-C to stop."
          % (what, roles, settle, seqmode, poll))
    print("  Watch for: a BEEP + any *** NEW *** post-injection frame + the lock itself.")
    if confirm_state:
        print("  CONFIRM: after the motor settles it polls cmd07 and reports the state")
        print("  readback vs the commanded state (%s) -- PASS/FAIL at the end."
              % confirm_state[1])
    print("  MODEL (bench 11-16/17 + captures, both directions confirmed):")
    print("  cmd03 = LOCK, cmd05 = UNLOCK (absolute; each motor-runs even when")
    print("  already in state). cmd09/cmd07/cmd2A/cmd4E are dialogue/ack-only.")
    print("  cmd07 reply data byte = state readout (01 locked / 02 unlocked).")
    seq = do_handshake(s, hello_timeout=hello_timeout, after=after,
                       poke_interval=poke_interval)
    if seq is None:
        return
    end = time.time() + seconds
    settle_at = time.time() + settle
    last_poll = 0.0
    injected = False
    inject_time = 0.0
    saw_complete = False
    complete_time = 0.0
    confirmed = False
    max_seq = 0x00
    baseline = {}      # (role,cmd) -> count, seen BEFORE the injection
    post = {}          # (role,cmd) -> count, seen AFTER the injection

    def _do_confirm():
        """Read the state back once the motor has settled; report PASS/FAIL."""
        nonlocal confirmed, seq
        print("\n>>> === CONFIRM: reading state back (cmd07) ===")
        state, seq = read_state(s, seq)
        if state is None:
            print("  ==> CONFIRM: no cmd07 reply -> INCONCLUSIVE (trust the bolt + LED)")
        else:
            ok = (state == confirm_state[0])
            print("  ==> RESULT: read data=%s -> %s   [%s]   (commanded %s)"
                  % (fmt_data([state]), state_word(state),
                     "PASS" if ok else "FAIL", confirm_state[1]))
        confirmed = True

    try:
        while time.time() < end:
            b = recv_frame(s, timeout=1.0)
            if b:
                f = log_frame("<-", b)
                if f:
                    rc = (f["fields"][2], f["fields"][3])
                    bucket = post if injected else baseline
                    bucket[rc] = bucket.get(rc, 0) + 1
                    max_seq = max(max_seq, f["fields"][1])     # fields[1] = SEQ
                    check_expected(f)
                    if injected:
                        if rc == (ROLE_MCU_CMD, 0x2A) and not saw_complete:
                            saw_complete = True               # motor-complete seen
                            complete_time = time.time()
                        if rc not in baseline:
                            print("    *** NEW post-injection frame: role=%02X cmd=%02X" % rc)
                        elif rc[0] == ROLE_MCU_CMD:
                            print("    (55 async frame, post-injection)")
            # keep steady state calm: periodic module status report (E7 cmd25)
            if poll and time.time() - last_poll >= poll:
                rcmd, rd = STEADY_REQ
                tx(s, build_frame(seq, ROLE_MOD, rcmd, rd),
                   "status cmd25 %s seq=%02X" % (fmt_data(rd), seq))
                seq = (seq + 1) & 0xFF
                last_poll = time.time()
            if not injected and time.time() >= settle_at:
                print("\n>>> === INJECTING %s roles=%s (seq=%s, base=%02X) ==="
                      % (what, roles, seqmode, max_seq))
                inject(s, mode, seqmode, max_seq, roles, frames)
                print(">>> === POST-INJECTION WATCH: listen for a BEEP ===\n")
                injected = True
                inject_time = time.time()
            # confidence rep: once the motor settles, read the state back
            if confirm_state and injected and not confirmed:
                due = (saw_complete and time.time() - complete_time >= CONFIRM_SETTLE) \
                      or (time.time() - inject_time >= CONFIRM_FALLBACK)
                if due:
                    _do_confirm()
    except KeyboardInterrupt:
        pass
    # best effort: if the loop ended before the confirm fired, run it once
    if confirm_state and injected and not confirmed:
        _do_confirm()
    print_act_summary(baseline, post)
