#!/usr/bin/env bash
set -euo pipefail

run_optional() {
  local title="$1"
  shift
  printf '%s\n' "$title"
  if "$@"; then
    true
  else
    printf '  status       unavailable\n'
  fi
}

printf 'ugreen_ax900 diagnostics\n'
printf '  kernel       %s\n' "$(uname -r)"
printf '  arch         %s\n' "$(uname -m)"

run_optional "usb devices" sh -c "lsusb | egrep -i 'a69c|368b|aic|ugreen|wifi|wireless' || true"
run_optional "usb tree" lsusb -t
run_optional "network links" sh -c "ip -br link | egrep 'wl|wlan|eno|eth|lo' || true"
run_optional "network manager" sh -c "nmcli device status 2>/dev/null || true"
run_optional "kernel modules" sh -c "lsmod | egrep 'aic|cfg80211' || true"
run_optional "dkms" sh -c "dkms status 2>/dev/null | grep -i aic || true"
run_optional "driver info" sh -c "modinfo aic8800_fdrv 2>/dev/null | egrep '^(filename|license|version|description|alias):' | sed -n '1,60p' || true"

printf 'kernel log\n'
if sudo -n true 2>/dev/null; then
  sudo dmesg -T | egrep -i 'aic|wlan|wifi|firmware|cfg80211|368b|a69c|rx_submit|probe|bus is not up' | tail -n 120 || true
else
  dmesg -T 2>/dev/null | egrep -i 'aic|wlan|wifi|firmware|cfg80211|368b|a69c|rx_submit|probe|bus is not up' | tail -n 120 || true
fi

