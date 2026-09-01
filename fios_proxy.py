#!/usr/bin/env python3
"""
fios_proxy.py - minimal ADB-tune + HDMI-encoder proxy for Channels DVR.

Runs on localhost alongside Channels DVR. Serves two things:
  GET /m3u             - generated M3U playlist for Channels DVR's Custom Channel source
  GET /channel/<num>   - tunes the Fios box to <num> via ADB, then streams the
                          encoder's RTSP feed back as MPEG-TS
  GET /status          - debug page: live snapshot, remote buttons, ADB/sleep
                          state, last-tuned channel, IP + forward/reverse DNS
                          for each device (see status.py)

Intentionally small and dependency-light (Flask only). Supports one or more
identical {box, encoder} tuner pairs via tuner_pool.py - "any idle tuner
serves any channel," no per-tuner pinning or channel affinity.

Setup:
  pip install flask --break-system-packages   # Debian 13 needs the flag
  edit lineup.json - single tuner: device_ip, encoder_rtsp, channels list.
  Multiple tuners: replace those two keys with a "tuners" list of
  {name, device_ip, encoder_rtsp, encoder_model} - any idle tuner in that
  list can serve any channel (see tuner_pool.py).
  python3 fios_proxy.py

Then in Channels DVR: Settings -> Add Source -> Custom Channels -> Add via URL
  http://127.0.0.1:5590/m3u
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort

from status import status_bp, mark_tuned, mark_stream_state
from tuner_pool import acquire, release, NoIdleTuner

CONFIG_PATH = Path(__file__).parent / "lineup.json"
app = Flask(__name__)
app.register_blueprint(status_bp)

# Displayed channel-number = this + the real Fios number (e.g. 603 -> 9603),
# so this source's numbers can't collide with anything else in the Channels
# DVR lineup. Only affects the M3U's displayed number - the /channel/<num>
# URL path (and therefore the ADB tune sequence) always uses the raw Fios
# number, since that's what the zero-padding in tune_to_channel expects.
CHANNEL_NUMBER_OFFSET = 9000


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def adb(device_ip, *args):
    subprocess.run(["adb", "-s", f"{device_ip}:5555", *args], check=False)


def tune_to_channel(device_ip, channel_number):
    """Wake the box and enter the (zero-padded to 4 digits) channel number."""
    padded = f"{int(channel_number):04d}"
    subprocess.run(["adb", "connect", f"{device_ip}:5555"], check=False, capture_output=True)
    adb(device_ip, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    time.sleep(0.3)
    for digit in padded:
        adb(device_ip, "shell", "input", "keyevent", f"KEYCODE_{digit}")
        time.sleep(0.3)
    adb(device_ip, "shell", "input", "keyevent", "KEYCODE_DPAD_CENTER")


def keepalive_loop(device_ip, stop_event, interval=3600):
    """While a stream is active, periodically nudge the box so its own
    idle-sleep timer (~a couple hours of no remote input) never fires mid
    watch. KEYCODE_WAKEUP, not KEYCODE_DPAD_CENTER - on an already-awake box
    it's a power-management no-op handled before it reaches the foreground
    player, so it can't pop an on-screen banner into the HDMI capture."""
    while not stop_event.wait(interval):
        adb(device_ip, "shell", "input", "keyevent", "KEYCODE_WAKEUP")


def sleep_device(device_ip):
    """Put the box to sleep. KEYCODE_SLEEP, not KEYCODE_POWER (which toggles
    and can wake an already-sleeping box instead)."""
    subprocess.run(["adb", "connect", f"{device_ip}:5555"], check=False, capture_output=True)
    adb(device_ip, "shell", "input", "keyevent", "KEYCODE_SLEEP")


def encoder_rtsp_url(config):
    """'encoder_rtsp' is the current name - any RTSP-capable encoder works
    here, not just a ZowieBox. 'zowiebox_rtsp' is honored as a deprecated
    alias so existing lineup.json files don't break on upgrade."""
    if "encoder_rtsp" in config:
        return config["encoder_rtsp"]
    if "zowiebox_rtsp" in config:
        print("WARNING: lineup.json uses the deprecated 'zowiebox_rtsp' key - "
              "rename it to 'encoder_rtsp' (same value, works with any "
              "RTSP-capable encoder).", file=sys.stderr)
        return config["zowiebox_rtsp"]
    raise KeyError("lineup.json is missing 'encoder_rtsp'")


def find_channel(config, number):
    for ch in config["channels"]:
        if ch["number"] == number:
            return ch
    return None


@app.route("/m3u")
def m3u():
    config = load_config()
    host = config["listen_host"]
    port = config["listen_port"]
    lines = ["#EXTM3U"]
    for ch in config["channels"]:
        if not ch.get("stationId"):
            # Channels DVR needs a real stationId to treat this as tunable -
            # without one it shows in the guide list but does nothing when
            # selected. Loud on purpose so a blank entry doesn't go unnoticed.
            print(f"WARNING: channel {ch['number']} ({ch['name']}) has no "
                  f"stationId - it will list but won't tune.", file=sys.stderr)
        station_tag = f' tvc-guide-stationid="{ch["stationId"]}"' if ch.get("stationId") else ""
        display_number = int(ch["number"]) + CHANNEL_NUMBER_OFFSET
        lines.append(
            f'#EXTINF:-1 channel-id="FIOS{display_number}" '
            f'channel-number="{display_number}"{station_tag},{ch["name"]}'
        )
        lines.append(f'http://{host}:{port}/channel/{ch["number"]}')
    return Response("\n".join(lines) + "\n", mimetype="audio/x-mpegurl")


@app.route("/channel/<number>")
def channel(number):
    config = load_config()
    ch = find_channel(config, number)
    if ch is None:
        abort(404, f"channel {number} not in lineup.json")

    # Reserved here, synchronously, rather than inside generate() - that way
    # "no idle tuner" comes back as a clean 503 instead of a half-started
    # streaming response. Whichever tuner comes back is exclusively ours
    # until release() in the finally block below, so no lock is needed
    # around tune_to_channel/ffmpeg startup - two requests can never hold
    # the same physical device at once.
    try:
        tuner = acquire(config)
    except NoIdleTuner as e:
        abort(503, str(e))

    def generate():
        try:
            tune_to_channel(tuner["device_ip"], number)
            mark_tuned(tuner["name"], number)
            time.sleep(config.get("settle_seconds", 2))

            proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-loglevel", "warning",
                    "-rtsp_transport", "tcp",
                    "-fflags", "+genpts",
                    "-i", tuner["encoder_rtsp"],
                    "-c:v", "copy",
                    # RTSP typically carries AAC as LATM (RFC 3640); MPEG-TS
                    # expects ADTS framing. Copying the audio bitstream
                    # straight across that mismatch is what silently drops
                    # it - re-encode audio only (cheap) to force valid ADTS.
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-f", "mpegts",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
            )
            keepalive_stop = threading.Event()
            keepalive_thread = threading.Thread(
                target=keepalive_loop,
                args=(tuner["device_ip"], keepalive_stop),
                daemon=True,
            )
            keepalive_thread.start()
            mark_stream_state(tuner["name"], True)
            try:
                while True:
                    chunk = proc.stdout.read(188 * 64)  # MPEG-TS packet size * batch
                    if not chunk:
                        break
                    yield chunk
            finally:
                mark_stream_state(tuner["name"], False)
                keepalive_stop.set()
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                sleep_device(tuner["device_ip"])
        finally:
            release(tuner["name"])

    return Response(generate(), mimetype="video/mp2t")


@app.route("/")
def index():
    return "fios_proxy is running. See /m3u for the Channels DVR playlist.\n"


if __name__ == "__main__":
    cfg = load_config()
    app.run(host=cfg["listen_host"], port=cfg["listen_port"], threaded=True)
