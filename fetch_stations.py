#!/usr/bin/env python3
"""
Pulls the full live-TV lineup directly from the VMS's UPnP ContentDirectory
service -- no ADB/STB needed. Walks every "Channels N thru M" container and
writes channels.json: [{"number": int, "name": str, "item_id": str}, ...]
"""
import argparse
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

DIDL_NS = {
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


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
             "number underneath -- only the displayed/GuideNumber shifts. Handy for "
             "keeping this test lineup from colliding with numbers your production "
             "Channels DVR lineup already uses.",
    )
    args = ap.parse_args()

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
            channels.append({
                "number": vms_number + args.number_offset,
                "vms_number": vms_number,
                "name": m.group(2).strip(),
                "item_id": it["id"],
            })

    channels.sort(key=lambda c: c["number"])
    print(f"[*] Total channels: {len(channels)}", file=sys.stderr)

    with open(args.out, "w") as f:
        json.dump(channels, f, indent=2)
    print(f"[*] Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
