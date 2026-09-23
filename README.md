# Fios TV+ → Channels DVR Proxy

Gets Verizon Fios TV+ live channels into [Channels DVR](https://getchannels.com/),
since Fios has no native LAN tuning API and Verizon is phasing out
CableCARD/HDHomeRun PRIME support in November 2026.

This repo contains **two independent proxies** that solve that problem two
different ways. They don't share code or config and you only need one
running — pick based on the status notes below. Full background, hardware
research, and investigation notes live in [`PROJECT_NOTES.md`](PROJECT_NOTES.md);
this file just orients you.

## The two approaches

| | `fios_proxy.py` | `dms_proxy.py` |
|---|---|---|
| Status | Working, in daily use | Experimental |
| How it gets video | Tunes a Stream TV box via ADB, captures its HDMI output with an RTSP encoder (e.g. ZowieBox), restreams as MPEG-TS | Pulls the stream directly from the household VMS's own media-server endpoint over the LAN |
| Extra hardware needed | One Stream TV box + one RTSP-capable HDMI encoder per simultaneous stream | None |
| Added to Channels DVR as | Custom Channel (M3U) source | HDHomeRun source |

`dms_proxy.py` exists because packet capture showed the VMS (the Verizon
CPE box that actually demuxes and serves live channels over HTTP) is doing
all the real work — the Stream TV box is just a remote-controlled client
pointed at a stream the VMS already produces. If pulling from the VMS
directly holds up, it would make the whole ADB/HDMI-capture pipeline (and
the encoder hardware it requires) unnecessary. It isn't there yet — see
its own section below — so `fios_proxy.py` remains the proven path.

## `fios_proxy.py` — ADB + HDMI capture

**Requirements:** Python 3, Flask, `ffmpeg`, `adb` (Android platform-tools),
one or more RTSP-capable HDMI encoders capturing each Stream TV box's
output.

**Setup:**
1. `pip install flask --break-system-packages` (the flag is needed on
   Debian 13)
2. Copy `lineup.json_example` to `lineup.json` and fill in your tuner(s)
   (`device_ip` for ADB, `encoder_rtsp` for the RTSP feed) and channel
   list. One tuner uses the flat `device_ip`/`encoder_rtsp` keys; more than
   one uses a `"tuners"` list instead — `tuner_pool.py` hands out whichever
   tuner is idle to serve a request, no per-tuner pinning.
3. `fetch_stations.py` looks up real Gracenote `stationId`s for your
   channels against Channels DVR's own station database — a channel
   without one lists in the guide but won't actually tune.
4. Run `python3 fios_proxy.py`, or install `fios_proxy.service` with
   systemd for it to run persistently.
5. In Channels DVR: Settings → Add Source → Custom Channels → Add via URL
   → `http://<listen_host>:<listen_port>/m3u`

**Endpoints:**
- `GET /m3u` — the M3U playlist Channels DVR reads
- `GET /channel/<num>` — tunes via ADB, then streams the encoder's feed
  back as MPEG-TS
- `GET /status` — live debug dashboard: per-tuner ADB/sleep state, last
  channel tuned, a virtual remote, forward/reverse DNS for both the box
  and the encoder side by side
- `GET /metrics` — Prometheus metrics (proxy/config health, per-tuner ADB
  and encoder reachability, busy/streaming state, cumulative stream time,
  tune/stream/error counts). Import `grafana/fios_proxy_dashboard.json`
  into Grafana for a ready-made dashboard.

## `dms_proxy.py` — direct from VMS (experimental)

Bypasses the Stream TV box, ADB, and HDMI capture entirely: it reads the
household VMS's UPnP ContentDirectory listing to find each channel's
`item_id`, then serves that channel's stream straight from the VMS's own
media-server endpoint. It presents itself to Channels DVR as an
HDHomeRun-compatible tuner rather than an M3U source.

**Why it's marked experimental, not a replacement yet:** the VMS has a
small, fixed number of real tuner resources shared with the rest of the
household's live TV — live testing during development briefly knocked a
different TV's stream offline while probing this endpoint (see
`PROJECT_NOTES.md`'s "Direct-from-VMS streaming" section for the full
writeup, including the safety note on further testing). `dms_proxy.py`'s
`MAX_CONCURRENT_STREAMS` is kept conservative until that risk is better
understood, and its use of the VMS's plain content-directory `item_id`
path (rather than the session-based endpoint that caused the collision)
has held up in testing so far, but hasn't been sanity-checked as
thoroughly as the ADB/HDMI pipeline has.

**Setup:**
1. `pull_lineup.py` browses the VMS's ContentDirectory and writes
   `channels.json`. Options worth knowing:
   - `--subscribed-file subscribed_channels.txt` (copy the `.example` and
     fill in your actual package) marks everything else `enabled: false`,
     so channels outside your subscription never show up in the guide or
     become streamable.
   - `--cdvr-host <channels-dvr-ip>` matches each enabled channel against
     Channels DVR's Gracenote/TMS station database, same idea as
     `fetch_stations.py` for the other pipeline.
2. Run `python3 dms_proxy.py`, or install `dms-proxy.service` with
   systemd.
3. In Channels DVR: Settings → Add Source → HDHomeRun → enter this
   host's IP. (Not a Custom Channel/M3U source — `dms_proxy.py` emulates
   an HDHomeRun's own discovery/lineup API.)

## Repo layout

| File | Purpose |
|---|---|
| `fios_proxy.py` | ADB+HDMI-capture proxy (Flask app, `/m3u`, `/channel`) |
| `status.py` | `/status` debug dashboard + shared tuner state, wired into `fios_proxy.py` |
| `metrics.py` | `/metrics` Prometheus endpoint, wired into `fios_proxy.py` |
| `tuner_pool.py` | Hands out idle tuners for `fios_proxy.py`'s multi-tuner pool |
| `fetch_stations.py` | Looks up Gracenote station IDs for `lineup.json` channels |
| `fios_tune.sh` | Standalone ADB tune/discover CLI, independent of the Flask app |
| `lineup.json_example` | Template for `fios_proxy.py`'s config (copy to `lineup.json`) |
| `fios_proxy.service` | systemd unit for `fios_proxy.py` |
| `grafana/fios_proxy_dashboard.json` | Grafana dashboard for `/metrics` |
| `dms_proxy.py` | Direct-from-VMS proxy (Flask app, HDHomeRun emulation) |
| `pull_lineup.py` | Builds `channels.json` for `dms_proxy.py` from the VMS's ContentDirectory |
| `subscribed_channels.example.txt` | Template for `pull_lineup.py --subscribed-file` |
| `dms-proxy.service` | systemd unit for `dms_proxy.py` |
| `PROJECT_NOTES.md` | Full project history: decisions, hardware research, investigation findings, open items |

`lineup.json`, `channels.json`, and `subscribed_channels.txt` are
git-ignored — they hold your actual network/channel details, not just
examples.

## License

MIT — see [`LICENSE`](LICENSE).
