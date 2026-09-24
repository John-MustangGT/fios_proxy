#!/usr/bin/env python3
"""
Pulls the full live-TV lineup directly from the VMS's UPnP ContentDirectory
service -- no ADB/STB needed. Walks every "Channels N thru M" container and
writes channels.json:

  [{"number": int,        # display/GuideNumber, after --number-offset
    "vms_number": int,    # real Fios channel number (what item_id is built from)
    "name": str,
    "item_id": str,
    "enabled": bool,       # from --subscribed-file; True for all if omitted
    "station_id": str|null,
    "match_method": str|null,     # hint_pin | exact_callsign | hd_suffix_guess |
                                   # needs_review | no_results | search_failed | null
    "review_candidate": str}, ...]  # only present for match_method == "needs_review":
                                     # top TMS search hit, NOT auto-applied -- confirm
                                     # it by hand, then promote it into a hints file
                                     # (see --hints-file / station_hints.csv) rather
                                     # than trusting it blind

The VMS hands back its ENTIRE possible lineup regardless of what you're
actually subscribed to -- --subscribed-file gates that down to just your
package, and station-ID matching only runs against enabled channels, so
we're not spamming Channels DVR's TMS search for channels you can't even
watch.

--hints-file (a small number,name,station_id CSV -- see station_hints.csv
at the repo root) is checked before any TMS search and always wins: it's
for channels you (or another proxy pulling the same real Fios lineup)
have already manually confirmed. TMS search only auto-applies a match for
the confident tiers (exact_callsign, hd_suffix_guess); anything murkier is
left as match_method "needs_review" with the top hit surfaced separately
in review_candidate, not silently written as the answer -- confirm it by
hand, then add it to your hints file so future runs skip the guesswork
for that channel entirely.
"""
import argparse
import csv
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

DIDL_NS = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


# ---------------------------------------------------------------- ContentDirectory

def soap_browse(vms_ip, vms_port, control_path, object_id, count=200):
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <ObjectID>{object_id}</ObjectID>
      <BrowseFlag>BrowseDirectChildren</BrowseFlag>
      <Filter>*</Filter>
      <StartingIndex>0</StartingIndex>
      <RequestedCount>{count}</RequestedCount>
      <SortCriteria></SortCriteria>
    </u:Browse>
  </s:Body>
</s:Envelope>""".encode("utf-8")

    url = f"http://{vms_ip}:{vms_port}{control_path}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def browse(vms_ip, vms_port, control_path, object_id):
    raw = soap_browse(vms_ip, vms_port, control_path, object_id)
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r"<Result>(.*)</Result>", text, re.S)
    if not m:
        raise RuntimeError("No <Result> in SOAP response:\n" + text[:500])
    didl_xml = html.unescape(m.group(1))
    root = ET.fromstring(didl_xml)

    containers = []
    for c in root.findall("didl:container", DIDL_NS):
        containers.append({"id": c.get("id")})

    items = []
    for it in root.findall("didl:item", DIDL_NS):
        title = (it.findtext("dc:title", default="", namespaces=DIDL_NS) or "").strip()
        items.append({"id": it.get("id"), "title": title})

    return containers, items


# ---------------------------------------------------------------- subscribed-channels file

def load_subscribed(path):
    """One VMS/real channel number per line. Blank lines and lines starting
    with # are ignored (# can also trail a number as a comment, e.g. '52 # TBS')."""
    numbers = set()
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                numbers.add(int(line))
            except ValueError:
                print(f"[!] Skipping unparsable line in {path!r}: {line!r}", file=sys.stderr)
    return numbers


# ---------------------------------------------------------------- manual hints file

def load_hints(path):
    """number,name,station_id CSV -> {vms_number: {"name":..., "station_id":...}}.
    See station_hints.csv at the repo root. Human-verified, so these always
    win over anything TMS search comes back with."""
    hints = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                number = int(row["number"])
            except (KeyError, TypeError, ValueError):
                print(f"[!] Skipping unparsable row in {path!r}: {row!r}", file=sys.stderr)
                continue
            station_id = (row.get("station_id") or "").strip()
            if not station_id:
                continue
            hints[number] = {"name": (row.get("name") or "").strip(), "station_id": station_id}
    return hints


def apply_hints(channels, hints):
    matched = 0
    for c in channels:
        if not c["enabled"]:
            continue
        hint = hints.get(c["vms_number"])
        if not hint:
            continue
        c["station_id"] = hint["station_id"]
        c["match_method"] = "hint_pin"
        matched += 1
    print(f"[*] {matched} channel(s) matched from hints file", file=sys.stderr)


# ---------------------------------------------------------------- Channels DVR TMS station matching

def clean_name(name):
    return re.sub(r"\bHD\b", "", name, flags=re.I).strip()


def normalize(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def query_tms(cdvr_host, cdvr_port, phrase):
    for candidate in (phrase, f'"{phrase}"'):
        # unquoted first (broader matching); some phrases (bare '&', etc.)
        # 503 unquoted and need to be retried wrapped in literal quotes
        quoted = urllib.parse.quote(candidate)
        url = f"http://{cdvr_host}:{cdvr_port}/tms/stations/{quoted}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 503 and candidate == phrase:
                continue
            raise
    return []


def best_match(name, results):
    """Returns (station_id, match_method, review_candidate).

    Only exact_callsign and hd_suffix_guess are confident enough to
    auto-assign a station_id. Anything murkier used to fall through to
    "first_result_guess" -- just taking TMS's top hit for a loose,
    unquoted keyword search -- which in practice was wrong often enough
    that it's not worth trusting automatically (see PROJECT_NOTES.md).
    That candidate is still surfaced, as review_candidate, so a human can
    confirm it -- just not auto-applied."""
    if not results:
        return None, "no_results", None
    target = normalize(clean_name(name))
    for r in results:
        if normalize(r.get("callSign", "")) == target:
            return r.get("stationId"), "exact_callsign", None
    for r in results:
        cs = normalize(r.get("callSign", ""))
        if cs == target + "hd" or (cs.endswith("hd") and cs[:-2] == target):
            return r.get("stationId"), "hd_suffix_guess", None
    return None, "needs_review", results[0].get("stationId")


def match_stations(channels, cdvr_host, cdvr_port):
    for c in channels:
        if not c["enabled"] or c.get("station_id"):
            continue  # already pinned via --hints-file -- never overwrite that
        print(f"[*] TMS search: {c['name']!r} (ch {c['number']}) ...", file=sys.stderr)
        try:
            results = query_tms(cdvr_host, cdvr_port, c["name"])
        except Exception as e:
            print(f"[!] TMS search failed for {c['name']!r}: {e}", file=sys.stderr)
            c["station_id"], c["match_method"] = None, "search_failed"
            continue
        station_id, method, review_candidate = best_match(c["name"], results)
        c["station_id"], c["match_method"] = station_id, method
        if review_candidate:
            c["review_candidate"] = review_candidate
        if method in ("needs_review", "no_results", "search_failed"):
            print(f"    [!] {method} -- review this one manually", file=sys.stderr)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vms-ip", default="192.168.1.101")
    ap.add_argument("--vms-port", type=int, default=8081)
    ap.add_argument("--control-path", default="/UD/2489c34a-1dd2-11b2-8555-a0e7ae9d382d?0")
    ap.add_argument("--out", default="channels.json")
    ap.add_argument(
        "--number-offset", type=int, default=0,
        help="Add this to every channel number in the output (e.g. 2000 turns 2 into "
             "2002, 1750 into 3750). item_id lookups still use the real VMS channel "
             "number underneath -- only the displayed/GuideNumber shifts.",
    )
    ap.add_argument(
        "--subscribed-file", default=None,
        help="Text file of VMS/real channel numbers you're actually subscribed to, "
             "one per line ('#' starts a comment). Channels not listed get "
             "enabled=false. Omit to leave everything enabled=true.",
    )
    ap.add_argument(
        "--hints-file", default=None,
        help="CSV of number,name,station_id manual overrides (see station_hints.csv "
             "at the repo root). Checked before TMS search and always wins -- for "
             "channels you've already confirmed by hand. Omit to skip.",
    )
    ap.add_argument(
        "--cdvr-host", default=None,
        help="Channels DVR server IP for Gracenote/TMS station-ID matching "
             "(e.g. 192.168.1.50). Omit to skip station matching entirely "
             "(station_id/match_method will be null).",
    )
    ap.add_argument("--cdvr-port", type=int, default=8089)
    args = ap.parse_args()

    subscribed = None
    if args.subscribed_file:
        subscribed = load_subscribed(args.subscribed_file)
        print(f"[*] Loaded {len(subscribed)} subscribed channel numbers from {args.subscribed_file}", file=sys.stderr)

    print(f"[*] Browsing root container on {args.vms_ip}:{args.vms_port} ...", file=sys.stderr)
    containers, _ = browse(args.vms_ip, args.vms_port, args.control_path, "0")
    print(f"[*] Found {len(containers)} channel-range containers", file=sys.stderr)

    channels = []
    seen = set()
    for cont in containers:
        cid = cont["id"]
        print(f"[*] Browsing \"{cid}\" ...", file=sys.stderr)
        try:
            _, items = browse(args.vms_ip, args.vms_port, args.control_path, cid)
        except Exception as e:
            print(f"[!] Failed to browse {cid}: {e}", file=sys.stderr)
            continue
        for it in items:
            m = re.match(r"^(\d+)\s*(.*)$", it["title"])
            if not m:
                print(f"[!] Skipping unparsable title: {it['title']!r} (id={it['id']})", file=sys.stderr)
                continue
            vms_number = int(m.group(1))
            if vms_number in seen:
                continue
            seen.add(vms_number)
            enabled = True if subscribed is None else (vms_number in subscribed)
            channels.append({
                "number": vms_number + args.number_offset,
                "vms_number": vms_number,
                "name": m.group(2).strip(),
                "item_id": it["id"],
                "enabled": enabled,
                "station_id": None,
                "match_method": None,
            })

    channels.sort(key=lambda c: c["number"])
    enabled_count = sum(1 for c in channels if c["enabled"])
    print(f"[*] Total channels: {len(channels)} ({enabled_count} enabled)", file=sys.stderr)

    if args.hints_file:
        hints = load_hints(args.hints_file)
        print(f"[*] Loaded {len(hints)} hint(s) from {args.hints_file}", file=sys.stderr)
        apply_hints(channels, hints)
    else:
        print("[*] No --hints-file given -- skipping manual overrides", file=sys.stderr)

    if args.cdvr_host:
        match_stations(channels, args.cdvr_host, args.cdvr_port)
    else:
        print("[*] No --cdvr-host given -- skipping station-ID matching", file=sys.stderr)

    tally = Counter(c["match_method"] for c in channels if c["enabled"])
    print(f"[*] Match method tally: {dict(tally)}", file=sys.stderr)

    with open(args.out, "w") as f:
        json.dump(channels, f, indent=2)
    print(f"[*] Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
