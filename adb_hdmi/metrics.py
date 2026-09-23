"""
metrics.py - Prometheus /metrics endpoint for fios_proxy.py.

Exposes proxy-level and per-tuner health/usage in Prometheus text
exposition format:
https://prometheus.io/docs/instrumenting/exposition_formats/

Kept dependency-light (no prometheus_client) to match the rest of this
project - it's a handful of gauges/counters, not worth a library.

ADB and encoder reachability are probed live on every scrape (the same
approach status.py's /status/<tuner>/info.json already uses, and that page
already polls every 3s) rather than reported from a cache, so a scrape
reflects what's actually true right now, not what was true last time
someone happened to load /status. PROBE_TIMEOUT keeps a dead tuner from
stalling a scrape for long; Prometheus's own scrape_timeout is the backstop
if a lineup somehow has enough tuners for that to matter.

Wired into fios_proxy.py as a Blueprint:
    from metrics import metrics_bp
    app.register_blueprint(metrics_bp)
"""

import socket
import time
import urllib.parse

from flask import Blueprint, Response

import status
from tuner_pool import busy_names

metrics_bp = Blueprint("metrics", __name__)

PROBE_TIMEOUT = 2  # seconds - short, this runs once per tuner on every scrape


def _esc(value):
    """Escape a label/comment value per the exposition format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _adb_reachable(device_ip, timeout=PROBE_TIMEOUT):
    from fios_proxy import _run_adb_connect  # deferred: avoids import cycle
    ok, _detail = _run_adb_connect(device_ip, timeout)
    return ok


def _tcp_reachable(host, port, timeout=PROBE_TIMEOUT):
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _help_type(lines, name, help_text, kind):
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")


@metrics_bp.route("/metrics")
def metrics():
    from fios_proxy import load_config  # deferred: avoids import cycle
    lines = []

    _help_type(lines, "fios_proxy_up",
                "Whether fios_proxy's config loaded successfully (0/1).", "gauge")
    try:
        config = load_config()
    except RuntimeError as e:
        lines.append("fios_proxy_up 0")
        lines.append(f"# lineup.json error: {_esc(e)}")
        return Response("\n".join(lines) + "\n",
                         mimetype="text/plain; version=0.0.4; charset=utf-8")
    lines.append("fios_proxy_up 1")

    tuners = status.get_tuners(config)
    busy = busy_names()
    now = time.time()

    _help_type(lines, "fios_proxy_channels",
                "Number of channels defined in lineup.json.", "gauge")
    lines.append(f"fios_proxy_channels {len(config.get('channels', []))}")

    _help_type(lines, "fios_proxy_tuners",
                "Number of tuners defined in lineup.json.", "gauge")
    lines.append(f"fios_proxy_tuners {len(tuners)}")

    _help_type(lines, "fios_proxy_tuners_busy",
                "Number of tuners currently reserved by a request.", "gauge")
    lines.append(f"fios_proxy_tuners_busy {len(busy)}")

    _help_type(lines, "fios_proxy_tuner_info",
                "Static info about a tuner (value is always 1) - join on the tuner label.",
                "gauge")
    _help_type(lines, "fios_proxy_tuner_adb_reachable",
                "Whether `adb connect` to the tuner's Fios box (its ADB port, 5555) "
                "succeeded just now (0/1).", "gauge")
    _help_type(lines, "fios_proxy_tuner_encoder_reachable",
                "Whether a TCP connection to the tuner's encoder RTSP port succeeded "
                "just now (0/1).", "gauge")
    _help_type(lines, "fios_proxy_tuner_busy",
                "Whether the tuner is currently reserved by a request (0/1).", "gauge")
    _help_type(lines, "fios_proxy_tuner_streaming",
                "Whether the tuner is actively streaming right now (0/1).", "gauge")
    _help_type(lines, "fios_proxy_tuner_stream_seconds_total",
                "Cumulative seconds this tuner has spent streaming, including any "
                "stream in progress right now.", "counter")
    _help_type(lines, "fios_proxy_tuner_last_tuned_seconds_ago",
                "Seconds since this tuner was last tuned. Absent if never tuned "
                "since fios_proxy started.", "gauge")
    _help_type(lines, "fios_proxy_tuner_tunes_total",
                "Total /channel requests served by this tuner.", "counter")
    _help_type(lines, "fios_proxy_tuner_streams_total",
                "Total streams started on this tuner.", "counter")
    _help_type(lines, "fios_proxy_tuner_stream_errors_total",
                "Streams on this tuner that ended having sent zero bytes - see the "
                "server log around that time for the ffmpeg error.", "counter")

    for t in tuners:
        name = t["name"]
        label = f'tuner="{_esc(name)}"'
        device_ip = t["device_ip"]
        parsed = urllib.parse.urlparse(t["encoder_rtsp"])
        encoder_port = parsed.port or 554

        adb_ok = _adb_reachable(device_ip)
        encoder_ok = _tcp_reachable(parsed.hostname, encoder_port)

        lines.append(
            f'fios_proxy_tuner_info{{{label},device_ip="{_esc(device_ip)}",'
            f'encoder_host="{_esc(parsed.hostname or "")}",'
            f'encoder_model="{_esc(t.get("encoder_model") or "")}"}} 1'
        )
        lines.append(f"fios_proxy_tuner_adb_reachable{{{label}}} {1 if adb_ok else 0}")
        lines.append(f"fios_proxy_tuner_encoder_reachable{{{label}}} {1 if encoder_ok else 0}")
        lines.append(f'fios_proxy_tuner_busy{{{label}}} {1 if name in busy else 0}')
        lines.append(
            f'fios_proxy_tuner_streaming{{{label}}} '
            f'{1 if status.ACTIVE_STREAM.get(name) else 0}'
        )
        lines.append(f"fios_proxy_tuner_stream_seconds_total{{{label}}} {status.stream_seconds(name)}")

        last = status.LAST_TUNED.get(name)
        if last is not None:
            lines.append(f'fios_proxy_tuner_last_tuned_seconds_ago{{{label}}} {now - last["at"]}')

        lines.append(f"fios_proxy_tuner_tunes_total{{{label}}} {status.TUNE_COUNT.get(name, 0)}")
        lines.append(f"fios_proxy_tuner_streams_total{{{label}}} {status.STREAM_COUNT.get(name, 0)}")
        lines.append(
            f"fios_proxy_tuner_stream_errors_total{{{label}}} "
            f"{status.STREAM_ERROR_COUNT.get(name, 0)}"
        )

    return Response("\n".join(lines) + "\n",
                     mimetype="text/plain; version=0.0.4; charset=utf-8")
