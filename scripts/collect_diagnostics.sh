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

show_firmware_info() {
  local firmware_dir="/lib/firmware/aic8800D80"
  local override="" firmware_file=""
  if [[ -e /sys/module/aic_load_fw/parameters/aic_fw_path ]]; then
    if [[ ! -r /sys/module/aic_load_fw/parameters/aic_fw_path ]]; then
      printf '  reason       cannot read aic_load_fw firmware path parameter\n'
      return 1
    fi
    IFS= read -r override < /sys/module/aic_load_fw/parameters/aic_fw_path || true
    [[ -z "$override" ]] || firmware_dir="$override"
  fi
  firmware_file="$firmware_dir/fmacfw_8800d80_u02.bin"
  printf '  path         %s\n' "$firmware_file"
  [[ -r "$firmware_file" ]] || return 1
  printf '  bytes        %s\n' "$(wc -c < "$firmware_file" | tr -d '[:space:]')"
  run_optional "firmware SHA256" sha256sum "$firmware_file"
  run_optional "firmware embedded version" sh -c \
    'strings "$1" | grep -E "20[0-9]{2}.* - g[0-9a-f]+"' sh "$firmware_file"
}

printf 'ugreen-ax900 diagnostics\n'
printf '  kernel       %s\n' "$(uname -r)"
printf '  arch         %s\n' "$(uname -m)"

run_optional "usb devices" sh -c "lsusb | egrep -i 'a69c|368b|aic|ugreen|wifi|wireless' || true"
run_optional "usb tree" lsusb -t
run_optional "network links" sh -c "ip -br link | egrep 'wl|wlan|eno|eth|lo' || true"
run_optional "network manager" sh -c "nmcli device status 2>/dev/null || true"
run_optional "kernel modules" sh -c "lsmod | egrep 'aic|cfg80211' || true"
run_optional "dkms" sh -c "dkms status 2>/dev/null | grep -i aic || true"
run_optional "driver info" sh -c "modinfo aic8800_fdrv 2>/dev/null | egrep '^(filename|license|version|description|alias):' | sed -n '1,60p' || true"
run_optional "installed D80 U02 firmware" show_firmware_info

printf 'kernel log\n'
log_pattern='aic|wlan|wifi|firmware|cfg80211|368b|a69c|rx_submit|probe|bus is not up|cmd timed-out|cmd queue crashed|bin upload fail|tkn\['
if sudo -n true 2>/dev/null; then
  sudo dmesg -T | egrep -i "$log_pattern" | tail -n 120 || true
else
  dmesg -T 2>/dev/null | egrep -i "$log_pattern" | tail -n 120 || true
fi

