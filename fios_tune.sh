#!/usr/bin/env bash
set -euo pipefail

DEVICE_IP="${2:-}"
CHANNEL="${3:-}"

adb_c() { adb -s "${DEVICE_IP}:5555" "$@"; }
key() { adb_c shell input keyevent "$1"; sleep "${2:-0.3}"; }

discover() {
  if [[ -z "$DEVICE_IP" ]]; then
    echo "Usage: $0 discover <device-ip>"; exit 1
  fi
  adb connect "${DEVICE_IP}:5555" || true
  adb devices -l
  adb_c shell pm list packages | grep -iE 'verizon|fios|stream|qlv' || true
}

tune_channel() {
  local ch
  ch=$(printf "%04d" "$1")   # Fios wants 4 digits: 553 -> 0553, 1553 stays 1553
  echo "Connecting to ${DEVICE_IP}:5555 ..."
  adb connect "${DEVICE_IP}:5555" >/dev/null

  echo "tune: entering channel ${ch}"
  for (( i=0; i<${#ch}; i++ )); do
    digit="${ch:$i:1}"
    key "KEYCODE_$digit" 0.3
  done

  key KEYCODE_DPAD_CENTER 0.5
  echo "Done. Tuned to channel ${ch}."
}

case "${1:-}" in
  discover) discover ;;
  tune)
    if [[ -z "$DEVICE_IP" || -z "$CHANNEL" ]]; then
      echo "Usage: $0 tune <device-ip> <channel-number>"; exit 1
    fi
    tune_channel "$CHANNEL"
    ;;
  *)
    echo "Usage:"
    echo "  $0 discover <device-ip>"
    echo "  $0 tune <device-ip> <channel-number>"
    exit 1
    ;;
esac
