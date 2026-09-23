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
      - Open question, not yet decided: does unifying the code also mean
        unifying the deployment host? `adb_hdmi/` runs on `channels`,
        `direct_vms/` currently runs separately on the kali box. Both need
        LAN reachability to the same Stream TV box IPs and the VMS, so
        co-locating them is plausible, but that's a separate decision from
        merging the code.
