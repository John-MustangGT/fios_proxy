"""
tuner_pool.py - hands out an idle {name, device_ip, encoder_rtsp, ...} tuner
per /channel/<num> request, now that there's more than one physical tuner.

All tuners are identical hardware/app, so this is deliberately dumb: no
per-tuner pinning, no channel affinity - just "first idle one wins." Reuses
status.py's get_tuners() for the actual tuner list, so a single-tuner
lineup.json (no "tuners" key) keeps working unchanged - get_tuners()
synthesizes a one-item list from the old flat device_ip/encoder_rtsp keys,
and a pool of one behaves exactly like the old hardcoded single-device path.
"""

import threading

from status import get_tuners

_lock = threading.Lock()
_busy = set()  # tuner names currently in use


class NoIdleTuner(Exception):
    pass


def acquire(config):
    """Reserve and return an idle tuner dict, or raise NoIdleTuner if every
    configured tuner is currently serving a stream."""
    tuners = get_tuners(config)
    with _lock:
        for t in tuners:
            if t["name"] not in _busy:
                _busy.add(t["name"])
                return t
    raise NoIdleTuner(f"all {len(tuners)} tuner(s) busy")


def release(tuner_name):
    with _lock:
        _busy.discard(tuner_name)


def busy_names():
    """For /status or debugging: the set of tuner names currently in use."""
    with _lock:
        return set(_busy)
