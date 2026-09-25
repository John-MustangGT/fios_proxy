# Fios TV+ / Channels DVR Integration — Project Notes

Single source of truth for decisions, confirmed facts, and deferred/future
work on this project. Update this file as things change rather than letting
decisions live only in chat history.

**2026-09-23:** repo reorganized -- the ADB/HDMI-capture files
(`fios_proxy.py`, `status.py`, `metrics.py`, `tuner_pool.py`,
`fetch_stations.py`, `fios_tune.sh`, `lineup.json_example`,
`fios_proxy.service`, `grafana/`) moved into `adb_hdmi/`; the
direct-from-VMS files (`dms_proxy.py`, `pull_lineup.py`,
`subscribed_channels.example.txt`, `dms-proxy.service`) moved into
`direct_vms/`. Paths mentioned below predate that move and are relative
to those new subdirectories now, not the repo root. See `README.md`.

**2026-09-23 (deployment topology, per John):** the repo doesn't make this
clear and should. Two separate Proxmox hosts -- **JSA is production,
Mirage is devel.** The `direct_vms/dms_proxy.py` deployment on the `kali`
box is **test-only**; production `dms_proxy.py` already runs colocated
with `fios_proxy.py` and Channels DVR itself, in the same `channels` LXC.
John's own words: "production/test environment isn't as clean as it should
be" -- so treat anything below this note about *which* host is which as
unconfirmed unless it's been rechecked, especially the "Confirmed working"
section's mention of the `channels` LXC running on host Mirage (may now be
stale if `channels` itself moved from Mirage to JSA at some point -- not
verified). **Resolved same day:** production's `/etc/systemd/system/dms-proxy.service`
on `channels` confirmed -- `User=root`, `Group=root`,
`WorkingDirectory=/root/Repo/fios_proxy` (pre-reorg path; now
`/root/Repo/fios_proxy/direct_vms`), venv at the repo root
(`/root/Repo/fios_proxy/venv`). `direct_vms/dms-proxy.service` now
matches this. No separate checked-in service file for `kali` -- per
John, `kali` is just run by hand (`python3 dms_proxy.py`) for testing,
not a systemd deployment.

**2026-09-24 (direct_vms station matching, per John):** John reported that
in `direct_vms/channels.json`, every match past `exact_callsign` was
wrong. Root cause: `pull_lineup.py`'s third matching tier,
`first_result_guess`, auto-wrote TMS's top search hit as the answer
whenever the first two (confident) tiers didn't hit -- and that search is
intentionally unquoted/broad for recall, so "top hit" was often just
whatever ranked highest for a loose keyword match, not the right channel.
Fixed: that tier no longer auto-writes anything. It's now `needs_review`
-- the candidate is still surfaced (`review_candidate` field) for a human
to check, never auto-trusted. See `pull_lineup.py`'s `best_match()`.

Also added `--hints-file`: a `number,name,station_id` CSV, checked before
any TMS search and always wins. `station_hints.csv` at the repo root is
seeded from a 117-channel list John provided of already-confirmed
`adb_hdmi` lineup entries -- this is very likely the same list
"Immediate next steps" below calls `channels_hdhr.csv` (same row count,
same HDHomeRun-PRIME-verified provenance), which resolves that open item:
it's now checked in and wired up, not just sitting unmerged.

**Related discovery, while tracing where adb_hdmi's Gracenote matching
actually happened:** the "94/94 matched" result described below under
"Full channel lineup with real Gracenote `stationId`s" was built by
`fetch_stations.py` + `build_lineup.py`, per that section's own
description (exact-callsign first, then HD-suffix heuristic, then flag
anything murkier for manual review -- the same two-confident-tiers
approach `pull_lineup.py` now matches). But `build_lineup.py` does not
exist anywhere in this repo's git history, and the `adb_hdmi/fetch_stations.py`
that IS checked in doesn't do TMS/Gracenote lookups at all -- it's just a
near-duplicate of the VMS ContentDirectory browser (the same code
`direct_vms/pull_lineup.py`'s `browse()` has). Whatever produced
`lineup.json_example`'s real `stationId`s is not present in this repo.
Not fixing that now -- flagging it so nobody goes looking for a matcher
in `fetch_stations.py` and wonders why it's not there.

**2026-09-25 (autotune.py, per John's field observation):** while running
`direct_vms/autotune.py --all` across the lineup looking for patterns,
John spotted that channels he's confirmed he's NOT subscribed to (e.g.
668/DestAm HD) come back `clear: mpeg2video` but with no resolution
printed -- every legitimately-tunable neighbor channel got a real
`WIDTHxHEIGHT`. That's a real gap in the classifier: `probe_with_ffprobe()`
was treating any non-empty ffprobe `streams` list as "has video," even
when `width`/`height` never came back -- so an unsubscribed channel's
degraded/incomplete response could still score `clear`, exactly the kind
of false positive the whole script exists to catch (same category as the
original 14-junk-bytes `producer_id` collision, just a subtler variant).
Fixed: a detected codec with no real dimensions is now treated the same
as no video found (`drm_or_blocked`), not auto-trusted. Also separately
confirmed unrelated to this: channel 553/FX plays fine on both the real
Stream TV box and `direct_vms`'s raw probe, but stalls on a Fire TV Stick
running the official Fios app (buffers, shows one frame, never advances)
-- looks like a Fire TV Stick/app decoder compatibility issue specific to
that device, not something in this repo's code; noted here in case a
pattern shows up later, not acted on further for now. Channels are also
confirmed to vary in codec (`mpeg2video` on several, `h264` in the
original Sept 22 packet capture) -- not one codec lineup-wide.

**Same day, follow-up -- pattern confirmed, and confirmed against real
ground truth:** John ran a full `--all` scan before the fix above had
been pulled into his working copy. Cross-referencing that run's
`channel_status.json` against `station_hints.csv`: 26 of 120 probed
channels came back `clear` with no resolution, and 25 of those 26 were
channels not present in `station_hints.csv` at all. The one apparent
exception -- 622/Science Channel, which *is* in `station_hints.csv` --
turned out not to be an exception: the real Stream TV box's own ZowieBox
capture feed shows Verizon's own on-screen message for that channel,
"Unsubscribed channel -- You are not subscribed to Science HD (622)."
So the pattern is actually 26/26, not 25/26. `station_hints.csv` having
an entry for 622 isn't wrong, it's just answering a different question
than it looks like -- that file only pins the correct Gracenote
`station_id` for a channel number/name, it was never a subscription
list (that's `--subscribed-file`'s job, which has never been populated
from real, verified data).

This means the no-resolution signal in `direct_vms/autotune.py` is a
validated, live, VMS-side subscription check, confirmed against the
app's own ground truth -- not just a strong correlation. See the new
TODO.md item about deriving `--subscribed-file`'s content from
`channel_status.json` automatically instead of hand-maintaining it.

## Goal

Get Verizon Fios TV+ (Stream TV / "Stream TV Cloud" Android TV box) live
channels into Channels DVR, since there's no native LAN control API and
CableCARD/HDHomeRun PRIME support is being phased out by Verizon in
November 2026.

## Architecture insight: where the "smarts" actually live

The Stream TV box (and the Fios TV app on Fire TV/Firestick — same
limitation observed there) needs LAN access to the household's VMS4100ATV
to do "Watch Live TV" at all. Working theory, now substantially confirmed
by packet capture (see "Direct-from-VMS streaming" below): the Stream TV
box is a bare Android TV box with the Fios TV app preloaded — it has no
real tuning/streaming smarts of its own. The ONT hands video channels off
to coax as normal QAM/RF, and the VMS4100ATV (Arris, `192.168.1.101` in
this household) is what actually receives that, demuxes it, and serves it
back out over the LAN as HTTP. The Stream TV box is little more than an
ADB-tunable remote-controlled client pointed at a stream the VMS already
produces.

This does not conflict with the project's goal — the VMS4100ATV is
Verizon-supplied CPE that's already present and required for Fios TV+ to
work at all, unlike the HDHomeRun PRIME + CableCARD path this project
exists to replace.

nmap of the VMS (`192.168.1.101`) turned up:
```
80/tcp    http
443/tcp   https
3000/tcp  (nmap's generic port-number guess, not a confirmed protocol)
7878/tcp  (not in original scan's top-1000 range — this is the real one)
8081/tcp  (generic guess)
9090/tcp  (generic guess)
9091/tcp  (generic guess)
9099/tcp  unknown
20000/tcp (generic guess)
30000/tcp (generic guess)
```
Arris CPE + this port spread is consistent with RDK-B/RDK-V middleware —
confirmed directly by packet capture: the VMS identifies itself as
`KreaTV HTTP Media Server` (Ericsson/CommScope IPTV middleware).

**ASIC tuner-count theory:** the original VMS design has 6 total tuners — 1
"internal use," 1 for DVR functions, 4 for mini-STBs. A companion box,
IPC-4100, adds 5 more mini-STB tuners. This household's unit is the newer
VMS4100ATV, which lacks the physical ports to itself act as a mini-STB, so
it might support 5 mini-STB tuners instead of 4. Bandwidth math confirms a
1GbE link is not the constraint regardless of codec assumptions (even
worst-case ~100 Mbps/stream × 5 = 500 Mbps, half of a gigabit link) — the
real ceiling is almost certainly the ASIC's tuner count, sized to normal
household usage (most homes run 2-3 STBs) rather than a bandwidth limit.
Notably, Verizon's "Streaming Unlimited" add-on is $20/mo for up to 5
streaming devices — the same "5" the ASIC math lands on. Likely not a
coincidence; both probably trace back to the same account-level
entitlement Verizon provisions, expressed two ways.

## Confirmed working (alpha, single box + single encoder)

**Hardware in use:**
- Stream TV box #1, SKU F3544K49203, Amlogic S905X4, Android TV, ADB at
  `192.168.1.105` (behind the Fios router's own 192.168.1.0/24 — no
  gateway assigned on that NIC)
- Stream TV box #2, ADB at `192.168.1.100`
- ZowieBox HDMI encoder #1 (the original free unit — turned out to be the
  $199 HDMI+NDI tier) on VLAN 4, `172.17.4.0/24`
- ZowieBox HDMI encoder #2 at `172.17.4.160`
- Channels DVR running in a dual-NIC unprivileged Proxmox LXC ("channels")
  on host Mirage — the `172.17.4.0/24` interface holds the default
  gateway, the `192.168.1.0/24` interface is static/no-gateway, used only
  to reach the Stream TV boxes directly

**ADB control (confirmed):**
- App family on the box: `com.verizon.qlv.*` (not `com.verizon.fios.*` as
  guessed early on)
  - `com.verizon.qlv.launcher` — home screen
  - `com.verizon.qlv.content` — guide/browse app
  - `com.verizon.qlv.services` — hosts `com.verizon.qlv.player.PlayerActivity`,
    the actual live-playback screen
  - `com.wbd.stream` — separate WBD app, not part of the Fios guide
- No deep-link/intent scheme exists or is published anywhere
- Channel tuning: 4-digit zero-padded number entered via
  `KEYCODE_0`.."KEYCODE_9"` keyevents, then `KEYCODE_DPAD_CENTER`
- `KEYCODE_WAKEUP`/`KEYCODE_SLEEP` used for power state — not
  `KEYCODE_POWER`, which toggles and can wake an already-sleeping box
- Hourly `KEYCODE_WAKEUP` keepalive while a stream is active, to prevent
  the box's own ~2hr idle-sleep timer from firing mid-watch.
  `KEYCODE_WAKEUP`, not `KEYCODE_DPAD_CENTER` — on an already-awake box
  it's a power-management no-op handled before it reaches the foreground
  player, so it can't pop an on-screen banner into the HDMI capture.
  `KEYCODE_SLEEP` sent on client disconnect to put the box back to sleep
  when nobody's watching.

**Proxy service (`fios_proxy.py`), Flask-based:**
- `GET /m3u` — generates the M3U playlist for a Channels DVR Custom
  Channel source
- `GET /channel/<num>` — tunes via ADB, then streams the encoder's RTSP
  feed back as MPEG-TS
- `GET /status` — debug page (see below)
- Config file `lineup.json`: `listen_host` (`172.17.4.234`, needs to be
  LAN-reachable for the Fire TV app, not `127.0.0.1`), `listen_port`
  (`5590`), `settle_seconds`, `channels` list, and either a single
  `device_ip`/`encoder_rtsp` pair or (once `TunerPool` shipped) a
  `"tuners"` list — see below
- `CHANNEL_NUMBER_OFFSET = 9000`: displayed channel number = this + the
  real Fios number (e.g. 603 → 9603), so this source's numbers can't
  collide with anything else in the Channels DVR lineup. Only affects the
  M3U's displayed number — the `/channel/<num>` URL path and ADB tune
  sequence always use the raw Fios number.
- Config key `encoder_rtsp` (any RTSP-capable encoder works, not just a
  ZowieBox specifically) — `zowiebox_rtsp` is honored as a deprecated
  alias so old `lineup.json` files don't break on upgrade
- `ffmpeg` invocation: `-rtsp_transport tcp -fflags +genpts -i
  <encoder_rtsp> -c:v copy -c:a aac -b:a 192k -f mpegts pipe:1`
  - `-rtsp_transport tcp` + `-fflags +genpts` fixes a "Timestamps are
    unset" warning
  - **Audio gotcha:** RTSP typically carries AAC as LATM/RFC 3640 framing;
    MPEG-TS expects ADTS. Blind `-c copy` for both streams silently
    drops/corrupts audio — re-encoding audio only (`-c:a aac`, cheap)
    forces valid ADTS framing while video stays a byte-for-byte copy.
- Channels DVR needs a real, Gracenote-matched `stationId` to treat a
  channel as tunable — a blank one shows in the guide list but does
  nothing when selected (not just cosmetic guide-data). `fios_proxy.py`
  prints a loud stderr warning for any channel missing one.

**systemd unit (`fios-proxy.service`)** installed and running —
`Type=simple`, `Restart=on-failure`, `After=network-online.target` +
`Wants=network-online.target` (avoids binding before the network is up).

**Full channel lineup with real Gracenote `stationId`s**, built via:
- `fetch_stations.py`: queries Channels DVR's real full-database search
  (`/tms/stations/<phrase>`) once per channel in `channels.csv` — **not**
  `/dvr/guide/stations`, which is scoped only to already-configured
  M3U/TVE sources and misses local OTA affiliates and most cable networks
  entirely regardless of guess quality (confirmed via the Channels
  community's own "Search for Gracenote Station ID's" thread). Search
  phrases are sent unquoted by default (broader OR-term matching), only
  retried wrapped in literal quotes on a 503 — some characters (a bare
  `&`, e.g. in "A&E") crash the endpoint unquoted, but quoting by default
  forces exact-phrase matching and hurts recall for fuzzy guesses.
- `build_lineup.py`: matches per-channel TMS search results into
  `lineup.json` (exact callSign match first, then HD-suffix heuristic,
  then flags ambiguous/missing entries for manual review)
- Result: 94/94 channels matched. **Known accepted gap:** HDNet Movies
  (Fios ch 746) doesn't appear in Gracenote's database at all — later
  explained, not just accepted as unsearchable: HDHomeRun PRIME's own
  Channels DVR channel-mapping UI (still functional pre-sunset) revealed
  channel 746 is now actually "Cowboy Channel," meaning HDNet Movies was
  discontinued/replaced at that slot, not merely hard to search for. That
  same HDHomeRun-sourced data also corrected Big Ten Network's real
  channel to 830 (not the originally-guessed 585). This authoritative
  HDHomeRun-derived list (`channels_hdhr.csv`, 117 rows) was produced but
  merging it fully into the working `channels.csv`/`lineup.json` was
  still pending as of last check — **verify this got done.**

**`/status` debug page (`status.py`)**, a Flask Blueprint registered into
`fios_proxy.py`. Per tuner:
- ADB connectivity (`adb devices` output) and sleep/wake state (parsed
  from `dumpsys power`'s `mWakefulness=`)
- A **Keep Awake** toggle: a single `KEYCODE_WAKEUP` lights the screen but
  doesn't reset the box's own idle timer, so it re-sleeps shortly after
  with nothing else going on. This toggle starts/stops a background
  thread re-sending `KEYCODE_WAKEUP` every 20s, independent of whether a
  real `/channel` stream is active — for hands-on debugging.
- Last-tuned channel, with a hover tooltip (native `title` attribute)
  showing name/call sign/station ID looked up from `lineup.json`
- A virtual remote (d-pad, back/home, sleep/wake, 0-9, ch+/-) posting to
  `/status/<tuner>/key/<key>`
- Both device IPs (Stream TV box + encoder) with forward **and** reverse
  DNS for each, shown side by side — the point being that once there's
  more than one tuner, it's otherwise easy to have ADB and the encoder
  pointed at two different physical units without noticing until it
  becomes a confusing symptom
- An `encoder_model` label (see `TunerPool` below)
- A one-click **"Open ZowieBox UI ↗"** link to the encoder's own native
  web UI in a new tab. An earlier in-page live-snapshot preview (grabbing
  single JPEG frames via a second `ffmpeg` RTSP client) was built, then
  deliberately dropped: the ZowieBox's own web UI turned out to not use a
  browser-native video format either — it ships raw H.264/H.265 to the
  browser (likely over a WebSocket) and decodes it client-side with
  `ffmpeg.wasm` onto a `<canvas>`. There's no simpler stream URL to grab,
  and native `ffmpeg` (already used elsewhere in this project) is a
  strictly better decoder than wasm-in-browser anyway, so linking straight
  to the ZowieBox's own UI beats reimplementing a worse version of it.
- Established debug workflow: three browser tabs side by side —
  `/status`, the ZowieBox's own UI, and the Channels DVR UI — covers the
  whole pipeline from one screen.

**Tuner pool (`tuner_pool.py`) — done.** Triggered by standing up the
second physical tuner. `acquire(config)` hands out the first idle
`{name, device_ip, encoder_rtsp, encoder_model}` dict (from `status.py`'s
`get_tuners()`), marks it busy; `release(name)` frees it in
`/channel/<num>`'s `finally` block. Deliberately dumb — first-idle-wins,
no per-tuner pinning or channel affinity, since all boxes are identical
hardware/app. `lineup.json` migration: replace the old top-level
`device_ip`/`encoder_rtsp` keys with a `"tuners"` list:
```json
"tuners": [
  {"name": "tuner1", "device_ip": "192.168.1.105", "encoder_rtsp": "rtsp://172.17.4.234:<port>/<path>", "encoder_model": "ZowieBox_hdmi"},
  {"name": "tuner2", "device_ip": "192.168.1.100", "encoder_rtsp": "rtsp://172.17.4.160:<port>/<path>", "encoder_model": "ZowieBox_hdmi"}
]
```
Old flat-key `lineup.json` files still work unchanged — `get_tuners()`
synthesizes a one-item pool from them, so a pool of one behaves exactly
like the previous hardcoded single-device path. `/channel/<num>` now
returns a clean 503 if every tuner is busy. The previously-anticipated
global `tune_lock` turned out to be unnecessary once the pool exists — a
tuner can never be double-acquired, so two requests can never hit the same
physical device concurrently — and was removed. `encoder_model` (e.g.
`"ZowieBox_hdmi"`) shipped as part of the same change: a no-op today since
every unit is identical, but it's the key to check once other encoder
hardware ever gets mixed in, without another schema migration.

**Field-confirmed**, not just logic that looked right on paper: pool
correctly hands out whichever tuner is idle, and per-tuner `sleep_device`
correctly puts only the tuner that was actually used to sleep. Real
multi-client walkthrough: TV was on Big Bang Theory (tuner A), phone tuned
to the Red Sox game (tuner B). Tuning the TV to the same game disconnected
its BBT stream (A went idle → slept) and its new request picked up the
just-freed tuner A for the game — both tuners briefly on the same channel,
which is expected (no session-sharing yet, see "Shared sessions" below),
not a bug. Turning the phone off released tuner B, which slept. Retuning
the phone to BBT later correctly picked the now-idle (sleeping) tuner B
and woke it via the normal path. Full idle/busy/sleep/wake bookkeeping
held up across a real two-viewer session with no manual intervention.

Note: requesting the same channel twice right now grabs a *second* tuner
rather than sharing the first one's stream — no channel affinity/session
sharing yet, see "Shared sessions" below.

## Hardware / purchasing decisions

ZowieBox pricing (Amazon, confirmed Aug 2026), three SKUs:
- **HDMI-only**: $180, 4KP30 encode, HDMI in/out/loop, RTSP/RTMP(s)/SRT
  out, no NDI.
- **HDMI+NDI**: $199, adds NDI|HX3 and RTSP/RTMP/SRT-to-NDI bridging.
  Still 4KP30 encode. This is the tier the original free unit turned out
  to be (has NDI settings in the web UI, but caps at 4K30 — not the Pro's
  4K60).
- **HDMI+NDI Pro**: $399, adds 4KP60 encode, NDI 6, touchscreen, AI edge
  tracking.

Since multi-viewer fan-out is already solved at the proxy/RTSP layer (no
NDI needed), the $180 HDMI-only model was the planned pick for additional
units: $180/channel vs. a LinkPi ENC5-V2's ~$65/channel (5-for-$325), but
each unit is a known-good, independently-replaceable tuner with no
shared-hardware blast radius and none of the LinkPi's freeze-bug/
default-open-telnet-and-ONVIF caveats found during research.

**DECIDED (2026-08-24): 4x ZowieBox HDMI-only, rack-mounted, LinkPi
dropped.** Ordered 3x HDMI-only ZowieBox to join the existing free unit —
one dedicated encoder per Stream TV box, 4 total, originally planned as
identical hardware/config. Also ordered rack infrastructure: DeskPi
RackMate T1 Plus 8U cabinet + a 9" rack-mount touchscreen monitor for
local console access.

**UPDATE (2026-08-25): fleet no longer fully homogeneous.** Amazon dropped
the HDMI+NDI tier $20, making it $9 *cheaper* than the HDMI-only unit
already paid for. Returned one HDMI-only unit, bought an HDMI+NDI unit
instead — this became the second-tuner unit. No functional impact:
`TunerPool` only cares about `{device_ip, encoder_url}`, and HDMI+NDI is a
strict superset (unused NDI settings, same RTSP path). Suspicion
(unconfirmed): HDMI-only vs. HDMI+NDI is likely the same board with NDI
gated behind a software/license flag, not a real BOM difference — the NDI
tier pricing *below* the "cheaper" tier only makes sense if the $180/$199
split was price segmentation from the start.

As of 2026-08-25 the HDMI+NDI unit was down further to $171 (limited-time
deal, list $191). Surveyed alternatives at that price point: nothing
comparable. A $159 4K@30 encoder/decoder and a $117 H.265 1080p box exist
but neither can act as a *decoder* back to an HDMI display the way the
ZowieBox can. A $199 HEVC encoder is close on paper (RTSP/RTMP/HTTP/UDP/
HLS/SRT out) but same gap — encode-only, no decode-back. ZowieBox stays
the pick. Worth rechecking pricing before ordering the remaining units —
if HDMI+NDI stays cheaper, no reason to keep buying HDMI-only at all.

**Multi-input encoder alternative, considered and not chosen for now:**
LinkPi ENC5-V2 (5x HDMI in, ~$325) — community-confirmed running "5
tuners at 60fps" from one unit, cheaper per-channel than 4+ separate
ZowieBoxes. Caveats: don't leave its own web console open on a
live-preview page while actively encoding (degrades performance); a
known freeze bug; default-open telnet/ONVIF. At exactly 4 boxes it's
barely worth it over 4 independent ZowieBoxes even before counting those
caveats, so it was dropped in the 2026-08-24 decision above.

**Basement network topology + recorder (design only, budget-dependent,
not ordered yet):** put a 1U MFF Dell and a small switch in the same rack
as the Stream TV boxes and ZowieBoxes, so ADB control traffic and
RTSP encoder→recorder traffic stay entirely on that local switch — a real
reduction from today's setup, where Fios-LAN traffic has to hairpin
through Mirage's second NIC to reach the "channels" LXC. If the Dell
becomes the new Channels DVR host, that's a migration off the Mirage LXC,
not a second DVR instance — decide explicitly, don't let it happen by
accident. Side benefit: Quick Sync transcode if the Dell has it. Caveat:
an MFF form factor needs a rack tray, not a native rack mount.

## Open architecture fork: ZowieBox-native NAS recording vs. Channels DVR
recording (not decided — evaluate after PoC)

ZowieBox supports writing a stream directly to a CIFS/SMB NAS share, and
exposes start/stop/pause/resume recording controls via a real API (used by
a published bitfocus Companion module). Limitation: box-native recording
has no per-program schedule — manual start/stop or continuous-loop only,
no EPG-driven scheduling. Decision point: does Channels DVR stay the
system-of-record for recordings (simpler, keeps existing DVR UI/apps), or
does a new custom scheduler/indexer take over to use the ZowieBox's own
recording path (which would make Channels DVR "just" a priority scheduler
and index, per the original idea that sparked this fork)? Not decided. Worth resolving
*before* spending on the basement rack/recorder hardware described above
under "Hardware / purchasing decisions" -- that build's design assumes an
answer to this question that hasn't actually been chosen yet.

## Sharing with the Channels DVR community (under consideration)

Worth posting to community.getchannels.com's "Hacks" category — no
existing Fios TV+/Stream TV integration was found when this started
(checked ADBTuner and hdmi-encoder-native-apps), and the CableCARD/
HDHomeRun PRIME sunset in November means a wave of other Fios subscribers
will be looking for exactly this. Real reusable content beyond "I made a
thing": the ADB digit-entry tuning approach, the RTSP→MPEG-TS AAC/LATM
audio gotcha, the `/tms/stations/` vs `/dvr/guide/stations` discovery, and
pulling verified callsigns from the HDHomeRun PRIME UI.

The project is already architecturally generic, not ZowieBox-specific:
ADB tuning is Fios/Stream-TV-specific, but the RTSP→MPEG-TS video path
works with any RTSP-capable encoder (config key `encoder_rtsp`, with a
deprecated `zowiebox_rtsp` alias for backward compatibility).

Pre-posting checklist: genericize `lineup.json` (strip real IPs/paths),
write a README, frame it as alpha/single-tuner (or now, current-pool-size).
**Open question, still undecided:** post now (single/dual-tuner) or wait
for the full planned pool.

## Future direction: shared sessions (the "beat Plex" idea) — deferred,
design only

Plex reserves one tuner per client even if two viewers are on the
identical channel at the same time (e.g. same game, two rooms) — a pure
limitation of how Plex's live TV client sessions are architected, not
something inherent to tuning. Since a tuned channel is just a stream,
there's no reason two viewers need two tuner reservations. Design: a
session registry keyed by channel number, sitting above the tuner pool —
a second request for a channel already being served by an idle-adjacent
tuner would attach to the existing stream instead of acquiring a new one.
Not started; `TunerPool` today has no channel affinity at all (confirmed
in the field test above — same channel on two tuners is the current,
expected behavior). Natural companion
to the unified-proxy work in `TODO.md` -- once there's one process
brokering both pipelines' tuners, a session registry sitting above it is
a smaller addition than bolting one onto two separate proxies would be.

## Direct-from-VMS streaming (investigated 2026-09-22 — real findings,
not just theory anymore)

**The idea:** if the VMS is doing all the actual channel demux/streaming
work, could the entire ADB-tune + HDMI-encoder-capture path be replaced by
pulling the stream straight from the VMS over the LAN? No Stream TV box,
no ADB, no HDMI capture, no per-channel hardware at all. This was
speculative going in; a real packet capture during a channel change
(`tune504.pcapng`, tap between the router and a Stream TV box, 16.8s,
9005 packets) settled several questions and raised one important new one.

**Confirmed facts from the capture:**
- **No IGMP or multicast anywhere.** Delivery is plain unicast TCP, same
  model as a real SiliconDust HDHomeRun (which also isn't multicast).
- **The real protocol:** plain HTTP/1.1. The Stream TV box does a `HEAD`
  probe, then a `GET`, both to:
  ```
  http://192.168.1.101:7878/mediasession?producer_id=19&type=tune_live&uhdls=false
  ```
  Server: `KreaTV HTTP Media Server` (confirms the RDK-B/RDK-V/Ericsson
  middleware guess). Client `User-Agent: VisualOn OSMP+ Player(Android
  TV)` — a commercial video player SDK. `uhdls=false` — confirms these are
  ordinary HD channels, not real 4K, consistent with the earlier bandwidth
  math.
- **DTCP-IP is offered but not used.** The response's `Content-Type`
  header advertises `application/x-dtcp1;DTCP1HOST=192.168.1.101;
  DTCP1PORT=7880;CONTENTFORMAT=video/mpeg` — DTCP-IP delivery is available
  as an option on a different port — but the client never touches port
  7880 or performs a DTCP-IP handshake; it just takes the plain version on
  7878.
- **Verified as real, unencrypted MPEG-TS**, not just assumed from
  appearances: byte-periodicity analysis found 13,807+ consecutive 0x47
  sync bytes at exactly 188-byte spacing (video PID `0x3E9`/1001, PUSI
  flag correctly distinguishing PES-start vs. continuation packets).
  Extracted the raw HTTP response body from the pcap and ran it through
  `ffprobe`/`ffmpeg`: confirmed real H.264, High profile, 1920×1080, and
  — the real test — decoded actual clean, watchable video frames straight
  from those bytes. No ADB, no HDMI, no ZowieBox involved in producing
  that image. This is about as conclusive as it gets that this specific
  delivery path is not DRM-wrapped at the container/PES level.
- **A short, unreadable TLS exchange to `192.168.1.101:443`** happens
  right before each tune. This is almost certainly the real "tune this
  producer to a channel" control-plane command. `producer_id` stayed the
  same (`19`) across a channel change within one device's session,
  suggesting the Stream TV box's own app fires this HTTPS call internally
  in response to the same ADB remote-control input we already send — we
  likely don't need to break that encryption to keep using ADB for tuning.

**Critical follow-up finding — `producer_id` is a stateful, exclusive
resource, not a free-for-all number (2026-09-22, live test from a third
LAN device, `.151`):**
- Requesting the exact `producer_id=19` captured earlier → `404 Not
  Found`. Most likely explanation: that value was scoped to the specific
  tune session captured, and had since expired/torn down (`/mediasession`
  really does mean "the currently active session for this tune," not a
  fixed hardware slot number).
- Requesting `producer_id=24` and `producer_id=25` → both returned `HTTP
  200` with the same DTCP1 headers, but only **14 junk bytes** of body
  (`00 a3 43 00 ...`, not valid TS data) before the connection closed.
  **At the same moment, a different TV in the house (living room) lost
  its stream.** This means those producer_ids were live, legitimately
  in use, and the bare `GET` request preempted/kicked the existing
  session without completing a real handoff of video to the new
  requester — worst of both outcomes.
- **Interpretation:** `producer_id` identifies one of the VMS's real,
  limited tuner resources (consistent with the ASIC tuner-count theory
  above). The plain-HTTP data-plane endpoint is not, by itself,
  sufficient to establish a working session from an arbitrary client —
  something from the encrypted control-plane exchange is required first.
  But the VMS trusts LAN clients enough that a bare, unauthenticated
  request can still forcibly preempt another device's active tuner
  claim, even though it can't complete its own session. That's a real,
  demonstrated collision risk, not a hypothetical one.

**SAFETY NOTE — read before doing any further live testing:** do not
sweep/guess `producer_id` values, or otherwise probe this endpoint live,
while other household members might be actively watching TV. This has
already been confirmed, in the field, to knock a real viewer's stream
offline. Any further testing here should be either (a) purely passive
packet capture (watch, don't request), or (b) live testing done only
during a window where it's known for certain nobody else is watching
anything, ideally targeting a tuner known to be idle rather than guessing.

**Current status of this thread:** genuinely promising — the data path
itself is confirmed unencrypted and directly consumable — but blocked on
the encrypted control-plane "tune" command, which is a materially bigger
and more invasive reverse-engineering task (likely requires an on-device
MITM with a cert-pinning bypass on the actual Android app) than passive
packet capture. This is **not** a replacement for the ADB+HDMI-capture
pipeline yet, and the production system is unaffected by any of this
investigation. Downshifted back to observe-only for now. If this ever
does pan out, it would fully obsolete the ZowieBox-farm hardware plan
above — worth continued investigation, just carefully.

## Immediate next steps / open items

1. Confirm whether `channels_hdhr.csv` (the HDHomeRun-PRIME-verified
   callsigns, including the Cowboy Channel/746 and Big Ten Network/830
   corrections) actually got merged into the live `channels.csv`/
   `lineup.json` — this was pending and its final status is unconfirmed.
2. Live-soak test: leave a channel open for several hours to confirm the
   hourly keepalive actually prevents idle-sleep, and that sleep-on-
   disconnect doesn't fire unexpectedly mid-stream.
3. Fill in the real `encoder_rtsp` URL for the second ZowieBox
   (`172.17.4.160`) in `lineup.json`'s `"tuners"` list (same port/path
   scheme as the first, just a different host).
4. Decide whether/when to post to community.getchannels.com — still
   undecided.
5. Direct-from-VMS: passive-only capture of the control-plane `:443`
   exchange during a tune, whenever it's safe to do so (see safety note
   above) — no more live `producer_id` guessing.
6. Recheck ZowieBox pricing before buying the remaining planned units.
