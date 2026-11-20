"""Command line: argparse + dispatch for all bench modes."""
import argparse
import os
import time

from . import link
from .frames import parse_csv, reconstruct
from .modes.act import act, cluster_idx, parse_frames_spec
from .modes.calibrate import calibrate, verify
from .modes.emulate import emulate
from .modes.manual import manual
from .modes.poll import poll
from .modes.poke import poke
from .modes.sniff import sniff

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("calibrate").add_argument("files", nargs="+")
    sub.add_parser("verify").add_argument("files", nargs="+")
    parsers = {}
    for m in ("sniff","emulate","act","lock","unlock","manual","poll","poke"):
        p = sub.add_parser(m)
        p.add_argument("-p","--port",required=True)
        p.add_argument("--swap",action="store_true")
        p.add_argument("--seconds",type=int,
                       default=(120 if m=="manual" else 300 if m=="poll" else 30))
        p.add_argument("-v","--verbose",action="store_true",
                       help="log every frame: timestamp + CRC ok/BAD (+ raw unframed bytes)")
        p.add_argument("--after",choices=["hello","cmd09"],default="cmd09",
                       help="handshake sync point: 'cmd09' = after the full prefix [default]; "
                            "'hello' = after frame one")
        p.add_argument("--hello-timeout",type=int,default=15,
                       help="seconds to wait for the 913's boot prefix before giving up")
        p.add_argument("--with-poke",action="store_true",
                       help="while waiting for the boot prefix, send a poke round (our E7 "
                            "request sequence + cmd07 + cmd12) every 3s instead of sitting "
                            "idle -- for a lock that was NOT power-cycled (no prefix expected); "
                            "on timeout, the replies seen distinguish 'fully silent' from the "
                            "post-autolock deaf state (cmd08/cmd09 only; PROTOCOL.md 14.12)")
        p.add_argument("--log",nargs="?",const="auto",default=None,metavar="FILE",
                       help="tee all output to a log file: bare --log = logs/<mode>_TIMESTAMP.log, "
                            "--log FILE = explicit path")
        parsers[m] = p
    pemu = parsers["emulate"]
    pemu.add_argument("--poll",type=float,default=2.0,
                      help="seconds between periodic cmd25 status reports (0 = disable)")
    pact = parsers["act"]
    pact.add_argument("--settle",type=int,default=8,help="seconds of handshake before injecting")
    pact.add_argument("--mode",choices=["cluster","single"],default="cluster",
                      help="cluster = full observed actuation sequence; single = one cmd4E frame (A/B test)")
    pact.add_argument("--seq",choices=["track","capture","zero"],default="track",
                      help="track = next-available SEQ (fixes stale-seq); capture = capture-3 SEQs; zero = 0x00")
    pact.add_argument("--roles",choices=["as-captured","e7"],default="as-captured",
                      help="as-captured = the cluster's original role bytes; e7 = force every "
                           "frame to role=E7 (the direction we send) -- the direction A/B")
    pact.add_argument("--poll",type=float,default=2.0,
                      help="seconds between periodic cmd25 status reports during the test (0 = quiet baseline)")
    pact.add_argument("--frames",default=None,metavar="LIST",
                      help="cluster mode only: comma-separated ACT_CLUSTER indices to inject, "
                           "e.g. --frames 4 = cmd03 (LOCK) alone; --frames 7 = cmd05 (UNLOCK) alone")
    # Convenience: drive one absolute state command (bench-confirmed both
    # directions: cmd03 = LOCK, cmd05 = UNLOCK).
    for m in ("lock","unlock"):
        pm = parsers[m]
        pm.add_argument("--settle",type=int,default=8,help="seconds of handshake before injecting")
        pm.add_argument("--seq",choices=["track","capture","zero"],default="track",
                        help="track = next-available SEQ (fixes stale-seq); capture = capture-3 SEQs; zero = 0x00")
        pm.add_argument("--poll",type=float,default=2.0,
                        help="seconds between periodic cmd25 status reports during the test (0 = quiet baseline)")
    pman = parsers["manual"]
    pman.add_argument("--poll",type=float,default=2.0,
                      help="seconds between periodic cmd25 status reports (0 = fully quiet listen; "
                           "the session may idle out -- the hello retry loop would then be visible)")
    ppoll = parsers["poll"]
    ppoll.add_argument("--interval",type=float,default=5.0,
                       help="seconds between cmd07 state polls")
    ppoll.add_argument("--keepalive",type=float,default=2.0,
                       help="seconds between cmd25 keepalive reports (0 = disable; the session "
                            "may idle out -- the hello retry loop would then be visible)")
    ppoll.add_argument("--settings",action="store_true",
                       help="each cycle also read the dipswitch settings (cmd12 = Z-Wave param 31, "
                            "read-only) and flag changes: 01=Lock Status LED 02=Autolock 04=Buzzer "
                            "08=Secure Screen (model-dependent)")
    ppoke = parsers["poke"]
    ppoke.add_argument("--rounds",type=int,default=10,
                       help="number of poke rounds (each = our E7 request sequence + cmd07 + cmd12)")
    ppoke.add_argument("--interval",type=float,default=3.0,
                       help="seconds between rounds (after the listen window)")
    ppoke.add_argument("--listen",type=float,default=2.0,
                       help="seconds to listen for replies after each round")
    a = ap.parse_args()
    if getattr(a, "log", None):
        logpath = a.log
        if logpath == "auto":
            os.makedirs("logs", exist_ok=True)
            logpath = "logs/%s_%s.log" % (a.cmd, time.strftime("%Y%m%d_%H%M%S"))
        link.install_tee(logpath)
        print("Logging to %s" % logpath)

    if a.cmd == "calibrate":
        frames = {}
        for fn in a.files:
            frames.update(reconstruct(parse_csv(fn)))
        calibrate(frames)
    elif a.cmd == "verify":
        frames = {}
        for fn in a.files:
            frames.update(reconstruct(parse_csv(fn)))
        verify(frames)
    else:
        link.VERBOSE = a.verbose
        poke_iv = 3.0 if a.with_poke else 0.0
        s = link.open_port(a.port, swap=a.swap)
        try:
            if a.cmd=="sniff": sniff(s, a.seconds)
            elif a.cmd=="emulate": emulate(s, seconds=a.seconds, hello_timeout=a.hello_timeout, after=a.after, poll_every=a.poll, poke_interval=poke_iv)
            elif a.cmd=="manual": manual(s, seconds=a.seconds, hello_timeout=a.hello_timeout, after=a.after, poll=a.poll, poke_interval=poke_iv)
            elif a.cmd=="poll": poll(s, seconds=a.seconds, interval=a.interval, keepalive=a.keepalive, settings=a.settings, hello_timeout=a.hello_timeout, after=a.after, poke_interval=poke_iv)
            elif a.cmd=="poke": poke(s, rounds=a.rounds, interval=a.interval, listen=a.listen)
            elif a.cmd=="act": act(s, seconds=a.seconds, settle=a.settle, mode=a.mode, seqmode=a.seq, hello_timeout=a.hello_timeout, after=a.after, poll=a.poll, roles=a.roles, frames=parse_frames_spec(a.frames), poke_interval=poke_iv)
            elif a.cmd in ("lock","unlock"):
                c = 0x03 if a.cmd == "lock" else 0x05
                st = (0x01, "LOCKED") if a.cmd == "lock" else (0x02, "unlocked")
                act(s, seconds=a.seconds, settle=a.settle, mode="cluster", seqmode=a.seq,
                    hello_timeout=a.hello_timeout, after=a.after, poll=a.poll, roles="e7",
                    frames=[cluster_idx(c)], label="%s (cmd%02X)" % (a.cmd.upper(), c),
                    confirm_state=st, poke_interval=poke_iv)
        except KeyboardInterrupt:
            print("\nstopped")
        s.close()