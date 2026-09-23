#!/usr/bin/env python3
"""
autotune.py -- per-channel direct-VMS playability probe.

Answers the question the TODO calls out: does direct_vms's plain
`item_id` stream for a given channel actually come back as real,
playable MPEG-TS/H.264 video -- not just an HTTP 200? The `producer_id`
collision investigation in PROJECT_NOTES.md is the cautionary example:
that request returned 200 with 14 junk bytes, not a real stream.

For each channel probed, this pulls a few seconds of the stream (same
URL dms_proxy.py itself streams from: http://VMS_IP:VMS_PORT/dms?item_id=...),
hands the bytes to ffprobe, and classifies the result as one of:

    clear           -- ffprobe found a real video stream
    drm_or_blocked  -- got a response, but no valid video came back
    error           -- request itself failed (timeout, connection
                       refused, non-200 status, etc.)

Results are written to a sibling file (default channel_status.json),
merged with whatever's already there -- so repeated small --range runs
accumulate into full lineup coverage over time instead of each run
wiping out the last one. channels.json itself is left untouched; it's
pull_lineup.py's file to regenerate, and merging autotune's output back
into it would just create a second way for the two scripts to fight
over the same file.

SAFETY -- read before running this against the real VMS:
  - This hits the *same* http://VMS_IP:VMS_PORT/dms?item_id=... endpoint
    dms_proxy.py streams from in production, which means each channel
    probed ties up one of the VMS's real, finite tuners for the
    duration of the probe (see dms_proxy.py's MAX_CONCURRENT_STREAMS
    comment). It is NOT the same endpoint (/mediasession?producer_id=)
    that was confirmed to knock a real viewer's stream offline during
    the Sept 22 investigation -- but "not the same known-dangerous
    endpoint" is not the same as "proven safe under contention."
  - Only run this during a window when nobody else in the house is
    watching TV. Prefer a small --range over --all while developing --
    a handful of known-mixed channels is enough to build and trust the
    classifier against without needing a full safe-testing window for
    every iteration.
  - Probes run strictly one at a time (never in parallel), each capped
    in both bytes and seconds, with a settle delay between channels so
    a freed tuner has time to actually release before the next request.

Usage:
    # see which channels a range/selector would hit, no network traffic
    python3 autotune.py --range 551-560 --dry-run

    # probe a small range (the recommended way to develop/trust this)
    python3 autotune.py --range 551-560

    # probe specific channels
    python3 autotune.py --numbers 551,555,830

    # probe the entire enabled lineup -- only during a safe window
    python3 autotune.py --all

    # skip the interactive safety confirmation (e.g. for a cron/timer
    # invocation that's already scheduled for a known-safe window)
    python3 autotune.py --range 551-560 --yes
"""
import argparse
import datetime
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Not imported from dms_proxy.py on purpose: importing it would run its
# module-level code (opens channels.json, imports Flask) just to grab two
# constants -- this stays dependency-free. Keep these in sync with
# dms_proxy.py's VMS_IP/VMS_PORT/LINEUP_FILE by hand (small, rarely-changed
# values; --vms-ip/--vms-port/--lineup below override them anyway).
VMS_IP = "192.168.1.101"
VMS_PORT = 7878
LINEUP_FILE = "channels.json"

STATUS_FILE = "channel_status.json"

# how much of a channel's stream to pull before giving up and closing --
# whichever limit hits first. Enough for ffprobe to confidently identify
# real H.264/MPEG-TS; nowhere near what a real viewing session would use.
DEFAULT_DURATION = 4.0        # seconds
DEFAULT_MAX_BYTES = 4_000_000  # ~4MB -- generous even for a few seconds of 1080p

DEFAULT_DELAY = 3.0  # seconds between channel probes, let the VMS settle

READ_CHUNK = 64 * 1024


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)
    sys.stderr.flush()


# ---------------------------------------------------------------- selection

def parse_selector(spec):
    """'551-560,600,620-625' -> sorted set of ints."""
    numbers = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            lo, hi = int(lo), int(hi)
            if lo > hi:
                lo, hi = hi, lo
            numbers.update(range(lo, hi + 1))
        else:
            numbers.add(int(chunk))
    return numbers


def select_channels(all_channels, args):
    enabled = [c for c in all_channels if c.get("enabled", True)]
    if args.all:
        return enabled
    wanted = set()
    if args.range:
        wanted |= parse_selector(args.range)
    if args.numbers:
        wanted |= parse_selector(args.numbers)
    return [c for c in enabled if c["vms_number"] in wanted]


# ---------------------------------------------------------------- probing

def fetch_sample(vms_ip, vms_port, item_id, duration=DEFAULT_DURATION,
                  max_bytes=DEFAULT_MAX_BYTES, timeout=10):
    """Pull up to `max_bytes` or `duration` seconds (whichever first) of a
    channel's stream into a temp file, then close the connection promptly
    so the VMS can release the tuner. Returns (path, bytes_read, error) --
    path is None on a request-level failure (error is set instead)."""
    url = f"http://{vms_ip}:{vms_port}/dms?item_id={item_id}"
    started = time.monotonic()
    total = 0
    tmp = tempfile.NamedTemporaryFile(prefix="autotune_", suffix=".ts", delete=False)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None, 0, f"HTTP {resp.status}"
            while True:
                if time.monotonic() - started >= duration or total >= max_bytes:
                    break
                chunk = resp.read(READ_CHUNK)
                if not chunk:
                    break
                tmp.write(chunk)
                total += len(chunk)
    except urllib.error.HTTPError as e:
        return None, 0, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, 0, f"connection failed: {e.reason}"
    except TimeoutError:
        return None, 0, "timed out"
    except Exception as e:  # noqa: BLE001 -- want any failure classified, not crash the run
        return None, 0, f"{type(e).__name__}: {e}"
    finally:
        tmp.close()
    return tmp.name, total, None


def probe_with_ffprobe(path):
    """Returns (has_video, detail) -- has_video False + a reason if ffprobe
    ran fine but found nothing playable."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "json",
                path,
            ],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH -- required by autotune.py")
    except subprocess.TimeoutExpired:
        return False, "ffprobe timed out analyzing the sample"

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return False, detail[-1] if detail else f"ffprobe exit {result.returncode}"

    try:
        parsed = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, "ffprobe produced unparseable output"

    streams = parsed.get("streams") or []
    if not streams:
        return False, "no video stream found"

    s = streams[0]
    codec = s.get("codec_name", "?")
    w, h = s.get("width"), s.get("height")
    detail = f"{codec} {w}x{h}" if w and h else codec
    return True, detail


def classify_channel(vms_ip, vms_port, chan, duration, max_bytes, timeout):
    path, nbytes, err = fetch_sample(vms_ip, vms_port, chan["item_id"], duration, max_bytes, timeout)
    try:
        if err:
            return "error", err
        if nbytes < 1024:
            # mirrors the producer_id collision case: HTTP 200 but only a
            # handful of junk bytes before the connection closed
            return "drm_or_blocked", f"only {nbytes} bytes received, no real stream"
        has_video, detail = probe_with_ffprobe(path)
        return ("clear" if has_video else "drm_or_blocked"), detail
    finally:
        if path:
            try:
                Path(path).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- results file

def load_status(path):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        eprint(f"[!] {path} is not valid JSON -- starting fresh (old copy left alone)")
        return {}


def save_status(path, status):
    Path(path).write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------- main

SAFETY_BANNER = """\
================================================================================
 This probes the live VMS directly (http://{ip}:{port}/dms?item_id=...) --
 the same endpoint dms_proxy.py streams real channels from in production.
 Each channel probed ties up one of the VMS's real, finite tuners for a
 few seconds.

 Only run this while nobody else in the house is watching TV.
================================================================================
"""


def main():
    ap = argparse.ArgumentParser(
        description="Probe direct-VMS channels for real playable video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument("--range", metavar="551-560",
                      help="VMS/real channel number range(s) to probe, e.g. "
                           "'551-560' or '551-560,600,620-625'.")
    sel.add_argument("--numbers", metavar="551,555,830",
                      help="Specific VMS/real channel numbers to probe (comma-separated).")
    sel.add_argument("--all", action="store_true",
                      help="Probe every enabled channel. Only during a safe window.")

    ap.add_argument("--vms-ip", default=VMS_IP, help=f"default: {VMS_IP}")
    ap.add_argument("--vms-port", type=int, default=VMS_PORT, help=f"default: {VMS_PORT}")
    ap.add_argument("--lineup", default=LINEUP_FILE, help=f"default: {LINEUP_FILE}")
    ap.add_argument("--out", default=STATUS_FILE, help=f"default: {STATUS_FILE}")
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION,
                     help=f"max seconds to sample per channel (default: {DEFAULT_DURATION})")
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                     help=f"max bytes to sample per channel (default: {DEFAULT_MAX_BYTES})")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                     help=f"seconds to wait between channels (default: {DEFAULT_DELAY})")
    ap.add_argument("--timeout", type=float, default=10.0,
                     help="per-request connect/read timeout in seconds (default: 10)")
    ap.add_argument("--yes", "-y", action="store_true",
                     help="skip the interactive safety confirmation")
    ap.add_argument("--dry-run", action="store_true",
                     help="print which channels would be probed and exit -- no network traffic")
    args = ap.parse_args()

    if not (args.range or args.numbers or args.all):
        ap.error("specify --range, --numbers, or --all (see --help)")

    with open(args.lineup) as f:
        all_channels = json.load(f)
    targets = select_channels(all_channels, args)

    if not targets:
        eprint("[!] Nothing matched that selector -- check the numbers against "
               f"{args.lineup} (vms_number field).")
        sys.exit(1)

    targets.sort(key=lambda c: c["vms_number"])
    eprint(f"[*] {len(targets)} channel(s) selected:")
    for c in targets:
        eprint(f"      {c['vms_number']:>5}  {c['name']}")

    if args.dry_run:
        eprint("[*] --dry-run: stopping here, no requests sent.")
        return

    eprint(SAFETY_BANNER.format(ip=args.vms_ip, port=args.vms_port))
    if not args.yes:
        reply = input("Type 'yes' to confirm nobody else is watching TV right now: ")
        if reply.strip().lower() != "yes":
            eprint("[*] Not confirmed -- exiting without touching the VMS.")
            sys.exit(1)

    status = load_status(args.out)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    for i, chan in enumerate(targets):
        key = str(chan["vms_number"])
        eprint(f"[*] Probing {chan['vms_number']} ({chan['name']}) ...")
        result, detail = classify_channel(args.vms_ip, args.vms_port, chan, args.duration, args.max_bytes, args.timeout)
        eprint(f"      -> {result}: {detail}")
        status[key] = {
            "number": chan["number"],
            "vms_number": chan["vms_number"],
            "name": chan["name"],
            "item_id": chan["item_id"],
            "status": result,
            "detail": detail,
            "checked_at": now,
        }
        save_status(args.out, status)  # write after every channel, not just at the end
        if i < len(targets) - 1:
            time.sleep(args.delay)

    clear = sum(1 for c in targets if status[str(c["vms_number"])]["status"] == "clear")
    eprint(f"[*] Done. {clear}/{len(targets)} clear. Results in {args.out}")


if __name__ == "__main__":
    main()
