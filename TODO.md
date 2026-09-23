# TODO

Working list of near-term and larger planned work. Smaller/completed
decisions and investigation history live in `PROJECT_NOTES.md` — this file
is just the actionable list.

## Packaging / deployment

- [ ] **LXC container.** A Proxmox LXC template/build script for deploying
      `adb_hdmi/` (needs `adb` + `ffmpeg` on the container) and/or
      `direct_vms/` (Python stdlib + Flask only, no extra system deps).
      Goal: `pct create`-and-go instead of manually installing packages and
      copying files onto a bare Debian LXC like today's `channels` host.
- [ ] **Docker container.** Same two targets. `adb_hdmi/` needs `adb` and
      `ffmpeg` baked into the image; no USB/device passthrough required
      either way since ADB here is always TCP (`device_ip:5555`), not USB —
      the container just needs LAN reachability to the Stream TV box IPs
      (and to the VMS, for `direct_vms/`).
- [ ] **Quick start instructions.** A short "clone, configure, run" path
      distinct from the README's fuller walkthrough and `PROJECT_NOTES.md`'s
      history — few enough steps to actually follow top-to-bottom without
      jumping around.
- [ ] **Formalize prod/devel config separation.** JSA (production) and
      Mirage (devel) currently rely on nobody mixing up which `lineup.json`/
      `channels.json` belongs where — John's own words, "production/test
      environment isn't as clean as it should be." A `deploy_env` field
      checked at startup (or just clearly separated example configs) would
      turn "accidentally ran devel config in prod" into a loud failure
      instead of a quiet one.

## Tooling

- [ ] **Autotune / channel-capability probe script.** Walk the lineup and
      determine, per channel, whether `direct_vms`'s plain `item_id` stream
      actually comes back as real playable video — not just an `HTTP 200`.
      The `producer_id` collision test in `PROJECT_NOTES.md` is the cautionary
      example: that request returned `200` with 14 junk bytes, not a real
      stream. A real check needs to pull a few seconds of data and confirm
      it's valid MPEG-TS/H.264 (`ffprobe`-style), same as the manual
      verification done during the Sept 22 investigation, just automated
      and run across the whole lineup instead of one channel by hand.
      Output: a clear/DRM-or-blocked/error classification per channel,
      written into `channels.json` (or a sibling file) so it's usable by a
      human today and by the router below once it exists.
      **Run this only during a window when nobody else is watching TV** —
      it's hitting the same VMS endpoint that knocked a real viewer's
      stream offline during manual testing.

- [ ] **M3U-driven Fios+ channel collection tag (HIGH PRIORITY, per John).**
      Right now telling which channels in a client's guide are "ours"
      means recognizing them by number/name one at a time -- a real pain
      in the FireTV app. Channels DVR's Channel Collections feature can
      group channels into their own guide filter (alongside the built-in
      Favorites/HD dropdown), and it supports genre-based "smart"
      collections driven by the Custom Channels M3U extension tag
      `tvc-guide-genres`. Plan: have `adb_hdmi/fios_proxy.py`'s `/m3u`
      generator (and `direct_vms/dms_proxy.py`'s lineup, once it emits
      similar metadata) tag every channel with something like
      `tvc-guide-genres="Fios+"`, then build one genre-based "Fios+"
      collection in the Channels DVR admin (one-time, manual -- there's
      no way to auto-create the collection itself, only auto-populate its
      membership via the tag). After that, every channel this project
      serves shows up under its own "Fios+" filter automatically, no
      per-channel picking. Do this alongside the autotune/channel-
      capability probe script above -- same pass over the lineup, and the
      probe's clear/DRM/error classification is a natural thing to fold
      into the tag too (e.g. flagging DRM'd channels) once it exists.

- [ ] **Confirm `channels_hdhr.csv` merge.** Data-hygiene loose end from
      `PROJECT_NOTES.md`'s "Immediate next steps": verify the
      HDHomeRun-PRIME-verified callsign corrections (Cowboy Channel/746,
      Big Ten Network/830) actually landed in the live `channels.csv`/
      `lineup.json`, not just the standalone `channels_hdhr.csv`. Quick to
      check, easy to forget.

- [ ] **Nightly lineup drift check (systemd timer).** A systemd
      timer that runs `pull_lineup.py` (or an equivalent VMS
      ContentDirectory browse) once a night, diffs the result against
      the checked-in/deployed `channels.json`, and reports anything
      that changed -- channels added or removed, `item_id` values that
      moved, name/number changes. Doesn't need to auto-apply anything;
      just surface the diff (log line, or a small summary written
      somewhere visible) so a VMS-side change doesn't silently break
      streaming until someone notices a channel is gone. Natural
      pairing with the autotune probe above once that exists -- same
      "ask the VMS what it has today" step, just compared against
      yesterday instead of turned into a classification.

## Observability & security

- [ ] **Alerting on top of `/metrics`.** The Grafana dashboard shows
      state but nothing pages anyone. A handful of Prometheus/Alertmanager
      rules — a tuner's ADB or encoder unreachable for N minutes, a burst
      of `fios_proxy_tuner_stream_errors_total` — would close the loop
      the dashboard opened: something should notice before a human does.
- [ ] **Backup/recovery path for `lineup.json` / `channels.json`.** Both
      are git-ignored on purpose (they hold real household IPs/paths), but
      that also means there's no recovery path today if the `channels`
      LXC's disk fails — nothing to restore from, just "rebuild it by
      hand." At minimum, document the from-scratch regeneration steps
      (`fetch_stations.py`/`pull_lineup.py`); ideally, an encrypted
      off-box copy somewhere.
- [ ] **Minimal auth on the proxy endpoints.** Fine today on a trusted
      home LAN with no exposure. Gets sharper once the unified proxy (see
      Architecture below) can trigger a live VMS `producer_id` request on
      `/channel` — that's the exact request class that already knocked a
      real viewer's stream offline once (see `PROJECT_NOTES.md`'s
      "Direct-from-VMS streaming" section). An unauthenticated LAN
      endpoint that can do that deserves more than "anyone on the LAN can
      hit it."

## Architecture

- [ ] **Unified proxy.** Merge `adb_hdmi/` and `direct_vms/` behind one
      process, one config file, and one lineup: for each channel, try the
      direct VMS stream first if the autotune probe marked it clear, verify
      it's actually producing video within a couple seconds, and fall back
      to acquiring a hardware tuner from the ADB+HDMI pool if it isn't (either
      because the channel's marked DRM/blocked, or the live check fails even
      though it was marked clear last time). This is the version worth
      building, not "always trust yesterday's probe result" — the VMS has
      already shown it can flake on a request that looks identical to a
      working one.

      Do this *after* the autotune script exists — there's no point
      designing the router against a guess about which channels are clear.

      Known reconciliation work, not blockers:
      - `tuner_pool.py` models an exclusive physical resource (one box, one
        stream); `direct_vms`'s `MAX_CONCURRENT_STREAMS` models a soft cap
        against the VMS's shared capacity. Both need to live under one
        `acquire()`/`release()`-style interface without losing either
        safety property.
      - Channel-numbering conventions differ today (`CHANNEL_NUMBER_OFFSET`
        in `adb_hdmi/` vs. `--number-offset` in `direct_vms/`). A unified
        lineup needs one logical channel identity per channel — a fallback
        should never look like a *second*, different channel to Channels
        DVR or Plex.
      - Resolved: this was never actually an open deployment question,
        just an unclear repo. `kali` is just run by hand for testing
        (`python3 dms_proxy.py`, no systemd unit) -- production already
        runs `dms_proxy.py` colocated with `fios_proxy.py`/Channels DVR in
        the `channels` LXC, same host, today, with no code changes.
        Unifying the code doesn't need a deployment-topology decision at
        all. `direct_vms/dms-proxy.service` matches production's actual
        config (`User=root`, confirmed from `channels`) -- see the dated
        note in `PROJECT_NOTES.md`.
