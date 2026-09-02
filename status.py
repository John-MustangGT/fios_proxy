"""
status.py - debug/status page for fios_proxy.py: live snapshot, remote
control buttons, and per-tuner health (ADB connectivity, sleep state, last
tuned channel, and forward/reverse DNS for each IP involved).

Split out from fios_proxy.py to keep the streaming path lean. This module
only touches shared state (LAST_TUNED / ACTIVE_STREAM) through the two
mark_*() helpers fios_proxy.py calls from its /channel/<num> route.

Deliberately built around a list of tuners (get_tuners()) rather than one
hardcoded device, even though today's lineup.json only has one - this is
exactly the case that gets hard to debug once there's a second box: is the
ADB device_ip and the encoder_rtsp host actually the SAME physical unit?
Each card below shows both IPs and their forward/reverse DNS side by side so
that mismatch is visible at a glance instead of discovered by symptom.

Wired into fios_proxy.py as a Blueprint:
    from status import status_bp, mark_tuned, mark_stream_state, DEFAULT_TUNER
    app.register_blueprint(status_bp)
"""

import socket
import subprocess
import threading
import time
import urllib.parse

from flask import Blueprint, Response, jsonify

status_bp = Blueprint("status", __name__)

DEFAULT_TUNER = "default"

_state_lock = threading.Lock()
LAST_TUNED = {}      # tuner_name -> {"channel": str, "at": epoch float}
ACTIVE_STREAM = {}   # tuner_name -> bool

# A single KEYCODE_WAKEUP wakes the screen but doesn't reset whatever idle
# timer the box uses to decide it's unattended - with nothing else going on
# (no keepalive thread, since that only runs during an active /channel
# stream) it just re-sleeps a short while later. This is a standalone,
# manually-toggled version of the same keepalive trick for debugging outside
# of a real stream.
_keepawake_lock = threading.Lock()
_keepawake_stop_events = {}  # tuner_name -> threading.Event, present iff running


def mark_tuned(tuner_name, channel):
    with _state_lock:
        LAST_TUNED[tuner_name] = {"channel": channel, "at": time.time()}


def mark_stream_state(tuner_name, active):
    with _state_lock:
        ACTIVE_STREAM[tuner_name] = active


def get_tuners(config):
    """List of {"name", "device_ip", "encoder_rtsp"} dicts. Reads
    config["tuners"] if present (future TunerPool shape); otherwise
    synthesizes a single-item list from today's flat top-level keys, so
    existing lineup.json files need no changes."""
    if "tuners" in config:
        return config["tuners"]
    from fios_proxy import encoder_rtsp_url  # deferred: avoids import cycle
    return [{
        "name": DEFAULT_TUNER,
        "device_ip": config["device_ip"],
        "encoder_rtsp": encoder_rtsp_url(config),
    }]


def _tuner_by_name(name):
    from fios_proxy import load_config
    config = load_config()
    return {t["name"]: t for t in get_tuners(config)}.get(name)


def _channel_info(config, number):
    """Look up name/callsign/stationId for a channel number, for the Last
    Channel tooltip. Tolerant of schema drift - lineup.json entries may or
    may not have a 'callsign' key depending on how they were built."""
    if number is None:
        return None
    for ch in config.get("channels", []):
        if ch.get("number") == number:
            return {
                "name": ch.get("name"),
                "callsign": ch.get("callsign"),
                "stationId": ch.get("stationId"),
            }
    return None


def _reverse_dns(ip):
    if not ip:
        return None
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


def _forward_dns(host):
    if not host:
        return None
    try:
        return socket.gethostbyname(host)
    except Exception:
        return None


def _keepawake_loop(device_ip, stop_event, interval=20):
    target = f"{device_ip}:5555"
    while not stop_event.wait(interval):
        subprocess.run(["adb", "-s", target, "shell", "input", "keyevent", "KEYCODE_WAKEUP"], check=False)


@status_bp.route("/status/<tuner>/keepawake/<state>", methods=["POST"])
def keepawake(tuner, state):
    t = _tuner_by_name(tuner)
    if t is None:
        return jsonify({"error": f"no tuner named {tuner}"}), 404
    if state not in ("on", "off"):
        return jsonify({"error": "state must be 'on' or 'off'"}), 400

    device_ip = t["device_ip"]
    with _keepawake_lock:
        running = _keepawake_stop_events.get(tuner)
        if state == "on":
            if running is None:
                connect = subprocess.run(
                    ["adb", "connect", f"{device_ip}:5555"], check=False, capture_output=True, text=True,
                )
                connect_output = (connect.stdout + connect.stderr).strip()
                if ("connected to" not in connect_output.lower()
                        and "already connected" not in connect_output.lower()):
                    return jsonify({"error": f"could not reach {tuner}: "
                                              f"{connect_output or 'no response'}"}), 502
                subprocess.run(
                    ["adb", "-s", f"{device_ip}:5555", "shell", "input", "keyevent", "KEYCODE_WAKEUP"],
                    check=False,
                )
                stop_event = threading.Event()
                threading.Thread(
                    target=_keepawake_loop, args=(device_ip, stop_event), daemon=True,
                ).start()
                _keepawake_stop_events[tuner] = stop_event
        else:
            if running is not None:
                running.set()
                del _keepawake_stop_events[tuner]
    return jsonify({"keep_awake": state == "on"})


def _adb_state(device_ip):
    """Returns (connectivity, power_state) strings, e.g. ("device", "Awake")."""
    target = f"{device_ip}:5555"
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return "adb unavailable", "unknown"

    connectivity = "not found"
    for line in out.splitlines():
        if line.startswith(target):
            parts = line.split(maxsplit=1)
            connectivity = parts[1].strip() if len(parts) > 1 else "unknown"
            break
    if connectivity != "device":
        return connectivity, "unknown"

    try:
        out = subprocess.run(
            ["adb", "-s", target, "shell", "dumpsys", "power"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("mWakefulness="):
                return connectivity, line.split("=", 1)[1]
    except Exception:
        pass
    return connectivity, "unknown"


@status_bp.route("/status")
def status_page():
    return Response(_render_page(), mimetype="text/html")


@status_bp.route("/status/<tuner>/info.json")
def tuner_info(tuner):
    from fios_proxy import load_config
    config = load_config()
    t = _tuner_by_name(tuner)
    if t is None:
        return jsonify({"error": f"no tuner named {tuner}"}), 404

    device_ip = t["device_ip"]
    parsed = urllib.parse.urlparse(t["encoder_rtsp"])
    encoder_host = parsed.hostname

    connectivity, power_state = _adb_state(device_ip)
    with _state_lock:
        last = LAST_TUNED.get(tuner)
        active = ACTIVE_STREAM.get(tuner, False)
    with _keepawake_lock:
        keep_awake = tuner in _keepawake_stop_events
    channel_info = _channel_info(config, last["channel"]) if last else None

    # encoder_host may be a raw IP or a hostname depending on lineup.json;
    # forward-resolve first so the PTR lookup always runs against the actual
    # IP either way (gethostbyname on an IP literal is a harmless no-op).
    encoder_ip = _forward_dns(encoder_host)

    return jsonify({
        "tuner": tuner,
        "encoder_model": t.get("encoder_model"),
        "device_ip": device_ip,
        "device_ptr": _reverse_dns(device_ip),
        "encoder_host": encoder_host,
        "encoder_ip": encoder_ip,
        "encoder_port": parsed.port,
        "encoder_ptr": _reverse_dns(encoder_ip),
        "adb_connectivity": connectivity,
        "power_state": power_state,
        "last_channel": last["channel"] if last else None,
        "last_channel_name": channel_info["name"] if channel_info else None,
        "last_channel_callsign": channel_info["callsign"] if channel_info else None,
        "last_channel_stationid": channel_info["stationId"] if channel_info else None,
        "last_tuned_ago_seconds": (time.time() - last["at"]) if last else None,
        "streaming_now": active,
        "keep_awake": keep_awake,
    })


# Dropped: an ffmpeg-grabbed still-frame preview used to live here. Turns
# out the ZowieBox's own web UI doesn't play its stream via a browser-native
# format either - it ships raw H.264/H.265 to the browser (likely over a
# WebSocket) and decodes it client-side with ffmpeg.wasm onto a <canvas>.
# There's no simpler stream URL to grab, and native ffmpeg (already used
# elsewhere in this project) is a strictly better decoder than wasm anyway -
# so for visual debugging, just link out to the ZowieBox's own UI instead of
# reimplementing a worse version of it here.


# ADB keycodes the remote grid below sends. Keep in sync with the HTML.
_REMOTE_KEYS = {
    "up": "KEYCODE_DPAD_UP", "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT", "right": "KEYCODE_DPAD_RIGHT",
    "center": "KEYCODE_DPAD_CENTER", "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME", "wake": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP",
    "chup": "KEYCODE_CHANNEL_UP", "chdown": "KEYCODE_CHANNEL_DOWN",
    "0": "KEYCODE_0", "1": "KEYCODE_1", "2": "KEYCODE_2", "3": "KEYCODE_3",
    "4": "KEYCODE_4", "5": "KEYCODE_5", "6": "KEYCODE_6", "7": "KEYCODE_7",
    "8": "KEYCODE_8", "9": "KEYCODE_9",
}


@status_bp.route("/status/<tuner>/key/<key>", methods=["POST"])
def send_key(tuner, key):
    t = _tuner_by_name(tuner)
    if t is None:
        return jsonify({"error": f"no tuner named {tuner}"}), 404
    keycode = _REMOTE_KEYS.get(key)
    if keycode is None:
        return jsonify({"error": f"unknown key {key}"}), 400

    device_ip = t["device_ip"]
    connect = subprocess.run(
        ["adb", "connect", f"{device_ip}:5555"], check=False, capture_output=True, text=True,
    )
    result = subprocess.run(
        ["adb", "-s", f"{device_ip}:5555", "shell", "input", "keyevent", keycode],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        error = (result.stderr.strip() or (connect.stdout + connect.stderr).strip()
                 or "adb command failed")
        return jsonify({"error": f"could not reach {tuner}: {error}"}), 502
    return jsonify({"sent": keycode})


def _render_page():
    from fios_proxy import load_config
    config = load_config()
    tuner_names = [t["name"] for t in get_tuners(config)]
    cards = "\n".join(f'<div class="card" data-tuner="{n}"></div>' for n in tuner_names)
    return f"""<!doctype html>
<html>
<head>
<title>Fios Proxy Status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 0; padding: 16px; }}
  h1 {{ font-size: 1.2rem; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 16px; }}
  .card {{ background: #1c1c1c; border: 1px solid #333; border-radius: 8px; padding: 12px; width: 340px; }}
  .card h2 {{ margin: 0 0 8px; font-size: 1rem; }}
  .zowie-link-btn {{ display: block; width: 100%; box-sizing: border-box; padding: 10px 0; margin-bottom: 8px;
                      background: #2a4d6b; color: #eee; text-align: center; text-decoration: none;
                      border-radius: 4px; font-weight: bold; }}
  .zowie-link-btn:hover {{ background: #35608a; }}
  .row {{ display: flex; justify-content: space-between; font-size: 0.85rem; margin: 2px 0; }}
  .row span:first-child {{ color: #999; }}
  .ok {{ color: #6f6; }} .bad {{ color: #f66; }}
  .hoverable {{ cursor: help; border-bottom: 1px dotted #888; }}
  .power-row {{ display: flex; gap: 6px; margin: 8px 0; }}
  .power-row button {{ flex: 1; padding: 8px 0; font-weight: bold; border: none; border-radius: 4px; cursor: pointer; }}
  .power-row .wake-btn {{ background: #2a6b2a; color: #eee; }}
  .power-row .wake-btn:hover {{ background: #357a35; }}
  .power-row .sleep-btn {{ background: #444; color: #eee; }}
  .power-row .sleep-btn:hover {{ background: #555; }}
  .keepawake-btn {{ width: 100%; margin: 4px 0 8px; padding: 8px 0; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; }}
  .ka-off {{ background: #444; color: #eee; }}
  .ka-on {{ background: #2a6b2a; color: #eee; }}
  .remote {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; margin-top: 10px; }}
  .remote button {{ padding: 8px 0; background: #2a2a2a; color: #eee; border: 1px solid #444; border-radius: 4px; cursor: pointer; }}
  .remote button:hover {{ background: #3a3a3a; }}
  .digits {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; margin-top: 6px; }}
</style>
</head>
<body>
<h1>Fios Proxy Status</h1>
<div class="cards">
{cards}
</div>
<script>
const CARD_TMPL = (n) => `
  <h2>${{n}} <span id="model-${{n}}" style="font-weight:normal;color:#888;font-size:0.75em;"></span></h2>
  <a id="zowielink-${{n}}" class="zowie-link-btn" href="#" target="_blank" rel="noopener">Open ZowieBox UI &#8599;</a>
  <div class="power-row">
    <button class="wake-btn" onclick="key('${{n}}','wake')">⏻ Wake</button>
    <button class="sleep-btn" onclick="key('${{n}}','sleep')">⏻ Sleep</button>
  </div>
  <button id="keepawake-${{n}}" class="keepawake-btn ka-off" data-state="off"
          onclick="toggleKeepAwake('${{n}}')">Keep Awake: OFF</button>
  <div class="row"><span>ADB</span><span id="adb-${{n}}">-</span></div>
  <div class="row"><span>Power</span><span id="power-${{n}}">-</span></div>
  <div class="row"><span>Last channel</span><span id="chan-${{n}}" class="hoverable">-</span></div>
  <div class="row"><span>Streaming now</span><span id="active-${{n}}">-</span></div>
  <div class="row"><span>Device IP</span><span id="devip-${{n}}">-</span></div>
  <div class="row"><span>Device PTR</span><span id="devptr-${{n}}">-</span></div>
  <div class="row"><span>Encoder host</span><span id="enchost-${{n}}">-</span></div>
  <div class="row"><span>Encoder IP</span><span id="encip-${{n}}">-</span></div>
  <div class="row"><span>Encoder PTR</span><span id="encptr-${{n}}">-</span></div>
  <div class="remote">
    <span></span><button onclick="key('${{n}}','up')">&uarr;</button><span></span>
    <button onclick="key('${{n}}','left')">&larr;</button><button onclick="key('${{n}}','center')">OK</button><button onclick="key('${{n}}','right')">&rarr;</button>
    <span></span><button onclick="key('${{n}}','down')">&darr;</button><span></span>
    <button onclick="key('${{n}}','back')">Back</button><button onclick="key('${{n}}','home')">Home</button><button onclick="key('${{n}}','sleep')">Sleep</button>
    <button onclick="key('${{n}}','wake')">Wake</button><button onclick="key('${{n}}','chdown')">Ch-</button><button onclick="key('${{n}}','chup')">Ch+</button>
  </div>
  <div class="digits">
    ${{[1,2,3,4,5,6,7,8,9,0].map(d => `<button onclick="key('${{n}}','${{d}}')">${{d}}</button>`).join('')}}
  </div>
`;

function statusClass(el, ok) {{ el.className = ok ? 'ok' : 'bad'; }}

async function refreshInfo(n) {{
  try {{
    const r = await fetch(`/status/${{n}}/info.json`);
    const d = await r.json();
    document.getElementById(`model-${{n}}`).textContent = d.encoder_model ? `(${{d.encoder_model}})` : '';
    document.getElementById(`adb-${{n}}`).textContent = d.adb_connectivity;
    statusClass(document.getElementById(`adb-${{n}}`), d.adb_connectivity === 'device');
    document.getElementById(`power-${{n}}`).textContent = d.power_state;
    const chanEl = document.getElementById(`chan-${{n}}`);
    chanEl.textContent = d.last_channel
      ? `${{d.last_channel}} (${{Math.round(d.last_tuned_ago_seconds)}}s ago)` : 'unknown';
    chanEl.title = d.last_channel
      ? `Name: ${{d.last_channel_name || '?'}}\nCall Sign: ${{d.last_channel_callsign || '?'}}\nStation ID: ${{d.last_channel_stationid || '?'}}`
      : '';
    document.getElementById(`active-${{n}}`).textContent = d.streaming_now ? 'yes' : 'no';
    const kaBtn = document.getElementById(`keepawake-${{n}}`);
    kaBtn.dataset.state = d.keep_awake ? 'on' : 'off';
    kaBtn.textContent = d.keep_awake ? 'Keep Awake: ON (tap to stop)' : 'Keep Awake: OFF (tap to hold)';
    kaBtn.className = `keepawake-btn ${{d.keep_awake ? 'ka-on' : 'ka-off'}}`;
    document.getElementById(`devip-${{n}}`).textContent = d.device_ip;
    document.getElementById(`devptr-${{n}}`).textContent = d.device_ptr || '(no PTR record)';
    document.getElementById(`enchost-${{n}}`).textContent = d.encoder_host;
    document.getElementById(`encip-${{n}}`).textContent = d.encoder_ip || '(no A record)';
    document.getElementById(`encptr-${{n}}`).textContent = d.encoder_ptr || '(no PTR record)';
    if (d.encoder_host) {{
      document.getElementById(`zowielink-${{n}}`).href = `https://${{d.encoder_host}}/`;
    }}
  }} catch (e) {{
    document.getElementById(`adb-${{n}}`).textContent = 'error';
  }}
}}

async function key(n, k) {{
  try {{
    const r = await fetch(`/status/${{n}}/key/${{k}}`, {{ method: 'POST' }});
    if (!r.ok) {{
      const d = await r.json().catch(() => ({{}}));
      alert(`Failed to send ${{k}} to ${{n}}: ${{d.error || r.status}}`);
    }}
  }} catch (e) {{
    alert(`Failed to send ${{k}} to ${{n}}: ${{e}}`);
  }}
}}

async function toggleKeepAwake(n) {{
  const btn = document.getElementById(`keepawake-${{n}}`);
  const turningOn = btn.dataset.state !== 'on';
  const r = await fetch(`/status/${{n}}/keepawake/${{turningOn ? 'on' : 'off'}}`, {{ method: 'POST' }});
  if (!r.ok) {{
    const d = await r.json().catch(() => ({{}}));
    alert(`Failed to toggle keep-awake for ${{n}}: ${{d.error || r.status}}`);
  }}
  refreshInfo(n);
}}

document.querySelectorAll('.card').forEach(card => {{
  const n = card.dataset.tuner;
  card.innerHTML = CARD_TMPL(n);
  refreshInfo(n);
  setInterval(() => refreshInfo(n), 3000);
}});
</script>
</body>
</html>
"""
