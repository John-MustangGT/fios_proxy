#!/usr/bin/env python3
"""
Direct-from-VMS Channels DVR Custom Channel proxy -- experimental.

Bypasses ADB/HDMI/ZowieBox entirely: serves live TV straight from the VMS's
DLNA endpoint (http://VMS_IP:7878/dms?item_id=channels_NNNNN) as an
HDHomeRun-style tuner Channels DVR can add as a source.

Needs channels.json from pull_lineup.py in the same directory.

SAFETY: the VMS has a small, finite number of real tuners. MAX_CONCURRENT_STREAMS
caps how many we'll open at once so this proxy can't itself ask for more than
the hardware can deliver. Kept conservative (2) until we've verified this doesn't
collide with anyone else's viewing -- raise it only after confirming that.

Run: pip install flask ; python3 dms_proxy.py
Then in Channels DVR: Add Source -> HDHomeRun -> enter this host's IP.
"""
import json
import logging
import threading
import time
import urllib.request

from flask import Flask, Response, abort, jsonify, request

VMS_IP = "192.168.1.101"
VMS_PORT = 7878
LINEUP_FILE = "channels.json"
MAX_CONCURRENT_STREAMS = 2
DEVICE_ID = "FIOSDMS0001"
FRIENDLY_NAME = "Fios Direct-VMS Tuner"
LISTEN_PORT = 5100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dms_proxy")
# quiet down Flask/Werkzeug's own per-request access log lines; we log the
# things that actually matter (stream start/stop) ourselves below
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)

_lock = threading.Lock()
_active = 0

with open(LINEUP_FILE) as f:
    _all_channels = json.load(f)
# only channels explicitly marked enabled (i.e. actually subscribed) are
# exposed -- the VMS's ContentDirectory returns its whole possible lineup
# regardless of your package, so pull_lineup.py's --subscribed-file gating
# is what keeps unsubscribed channels out of the guide entirely
CHANNELS = [c for c in _all_channels if c.get("enabled", True)]
BY_NUMBER = {str(c["number"]): c for c in CHANNELS}


@app.route("/discover.json")
def discover():
    base = request.host_url.rstrip("/")
    return jsonify({
        "FriendlyName": FRIENDLY_NAME,
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomeruntc_atsc",
        "FirmwareVersion": "20200101",
        "DeviceID": DEVICE_ID,
        "DeviceAuth": "test",
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
        "TunerCount": MAX_CONCURRENT_STREAMS,
    })


@app.route("/lineup_status.json")
def lineup_status():
    return jsonify({
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
    })


@app.route("/lineup.json")
def lineup():
    base = request.host_url.rstrip("/")
    return jsonify([
        {
            "GuideNumber": str(c["number"]),
            "GuideName": c["name"] or f"Ch {c['number']}",
            "URL": f"{base}/stream/{c['number']}",
        }
        for c in CHANNELS
    ])


@app.route("/auto/v<int:channel>")
@app.route("/stream/<int:channel>")
def stream(channel):
    global _active
    chan = BY_NUMBER.get(str(channel))
    client = request.remote_addr
    if not chan:
        log.warning("404 unknown channel %s requested by %s", channel, client)
        abort(404, f"Unknown channel {channel}")

    with _lock:
        if _active >= MAX_CONCURRENT_STREAMS:
            log.warning(
                "503 refused ch %s (%s) for %s -- all %d tuners busy",
                channel, chan["name"], client, MAX_CONCURRENT_STREAMS,
            )
            abort(503, f"All {MAX_CONCURRENT_STREAMS} tuners busy")
        _active += 1
        active_now = _active
    log.info(
        "STREAM START ch %s (%s) client=%s active=%d/%d",
        channel, chan["name"], client, active_now, MAX_CONCURRENT_STREAMS,
    )

    url = f"http://{VMS_IP}:{VMS_PORT}/dms?item_id={chan['item_id']}"

    def generate():
        global _active
        started = time.monotonic()
        total_bytes = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=15) as upstream:
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    yield chunk
        except Exception as e:
            log.error("STREAM ERROR ch %s (%s) client=%s: %s", channel, chan["name"], client, e)
        finally:
            duration = time.monotonic() - started
            with _lock:
                _active -= 1
                active_now = _active
            log.info(
                "STREAM END ch %s (%s) client=%s duration=%.1fs bytes=%d active=%d/%d",
                channel, chan["name"], client, duration, total_bytes, active_now, MAX_CONCURRENT_STREAMS,
            )

    return Response(generate(), mimetype="video/mpeg")


if __name__ == "__main__":
    log.info("%d of %d channels enabled from %s", len(CHANNELS), len(_all_channels), LINEUP_FILE)
    log.info("listening on 0.0.0.0:%d (max %d concurrent streams)", LISTEN_PORT, MAX_CONCURRENT_STREAMS)
    app.run(host="0.0.0.0", port=LISTEN_PORT, threaded=True)
