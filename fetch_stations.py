#!/usr/bin/env python3
"""
fetch_stations.py - query Channels DVR's real full-database station search
(/tms/stations/<phrase>) once per channel in channels.csv, instead of
matching against the limited /dvr/guide/stations dump (which only covers
stations already tied to your configured M3U/TVE sources - local OTA
affiliates and most cable networks are never in there no matter how good
the callsign guess is).

Run this ON the Channels DVR box (or anywhere that can reach it on 8089).

Usage:
    python3 fetch_stations.py channels.csv > tms_results.json
    python3 fetch_stations.py channels.csv --host 172.17.4.234 --port 8089 -o tms_results.json

channels.csv format is the same as before: number,name,callsign - callsign
is used as the search phrase when present (more precise), otherwise name is
used with common noise ("HD", parenthetical notes) stripped.
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def clean_search_term(name):
    """Strip trailing ' HD' and any '(...)' aside, e.g. 'WGBH (PBS)' -> 'WGBH'."""
    name = re.sub(r"\s*\([^)]*\)\s*", "", name)
    name = re.sub(r"\s+HD$", "", name, flags=re.IGNORECASE)
    return name.strip()


def fetch_once(host, port, phrase, timeout):
    url = f"http://{host}:{port}/tms/stations/{urllib.parse.quote(phrase)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(host, port, phrase, timeout=10):
    # Unquoted by default: multi-word phrases get OR'd term-by-term
    # server-side, which is more forgiving when you're guessing at wording
    # rather than quoting an exact database name. Only fall back to a
    # quoted exact-phrase search (wrapped in literal " then percent-encoded)
    # if the plain query 503s - some characters (observed with a bare '&',
    # e.g. "A&E") seem to get parsed as search-syntax operators server-side
    # and crash the endpoint rather than erroring cleanly.
    try:
        return fetch_once(host, port, phrase, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 503:
            print(f"  ERROR querying {phrase!r}: {e}", file=sys.stderr)
            return []
        try:
            return fetch_once(host, port, f'"{phrase}"', timeout)
        except Exception as e2:
            print(f"  ERROR querying {phrase!r} (quoted retry also failed): {e2}", file=sys.stderr)
            return []
    except Exception as e:
        print(f"  ERROR querying {phrase!r}: {e}", file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channels_csv")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default=8089, type=int)
    ap.add_argument("--delay", default=0.3, type=float, help="seconds between requests")
    ap.add_argument("-o", "--output", default=None, help="write JSON here instead of stdout")
    args = ap.parse_args()

    out = {}
    with open(args.channels_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for i, row in enumerate(rows):
        number = row["number"].strip()
        name = row["name"].strip()
        callsign = (row.get("callsign") or "").strip()
        phrase = callsign or clean_search_term(name)

        print(f"[{i+1}/{len(rows)}] {number} {name!r} -> searching {phrase!r}", file=sys.stderr)
        results = fetch(args.host, args.port, phrase)
        out[number] = {"query": phrase, "name": name, "results": results}
        time.sleep(args.delay)

    text = json.dumps(out, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")
        print(f"Wrote {len(out)} channels' worth of search results to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
