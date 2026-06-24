#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_NAME="aic8800"
PACKAGE_VERSION="1.0.9"
SOURCE_DIR="$ROOT/src/aic8800-1.0.9"
FIRMWARE_DIR="$ROOT/firmware/aic8800D80"
UDEV_RULE="$ROOT/udev/aic.rules"
KERNEL_VERSION="${KERNEL_VERSION:-$(uname -r)}"

print_block() {
  local title="$1"
  shift
  printf '%s\n' "$title"
  while [ "$#" -gt 0 ]; do
    printf '  %-12s %s\n' "$1" "$2"
    shift 2
  done
}

fail() {
  print_block "build_from_source result" "status" "failed" "reason" "$1"
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令 $1"
}

check_environment() {
  [ "$(id -u)" -eq 0 ] || fail "请使用 sudo 执行"
  [ -d "$SOURCE_DIR" ] || fail "缺少源码目录 $SOURCE_DIR"
  [ -d "$FIRMWARE_DIR" ] || fail "缺少固件目录 $FIRMWARE_DIR"
  [ -d "/lib/modules/$KERNEL_VERSION/build" ] || fail "缺少内核头文件 /lib/modules/$KERNEL_VERSION/build"
  need_cmd dkms
  need_cmd make
  need_cmd gcc
  need_cmd depmod
}

install_firmware_and_rules() {
  local backup_dir="/var/backups/ugreen_ax900/source-build-$(date +%Y%m%d%H%M%S)"
  install -d -m 0755 "$backup_dir" /lib/firmware /usr/lib/udev/rules.d /etc/modules-load.d

  if [ -d /lib/firmware/aic8800D80 ]; then
    cp -a /lib/firmware/aic8800D80 "$backup_dir/aic8800D80"
  fi
  rm -rf /lib/firmware/aic8800D80
  cp -a "$FIRMWARE_DIR" /lib/firmware/aic8800D80

  if [ -f /usr/lib/udev/rules.d/aic.rules ]; then
    cp -a /usr/lib/udev/rules.d/aic.rules "$backup_dir/aic.rules"
  fi
  install -m 0644 "$UDEV_RULE" /usr/lib/udev/rules.d/aic.rules
  printf 'aic_load_fw\naic8800_fdrv\n' > /etc/modules-load.d/aic8800.conf
  udevadm control --reload || true

  print_block "build_from_source firmware" "status" "installed" "backup" "$backup_dir"
}

build_driver() {
  local dst="/usr/src/$PACKAGE_NAME-$PACKAGE_VERSION"
  local src_backup="/var/backups/ugreen_ax900/usr-src-$PACKAGE_VERSION-$(date +%Y%m%d%H%M%S)"

  modprobe -r aic8800_fdrv 2>/dev/null || true
  modprobe -r aic_load_fw 2>/dev/null || true

  if [ -d "$dst" ]; then
    install -d -m 0755 "$(dirname "$src_backup")"
    cp -a "$dst" "$src_backup"
    rm -rf "$dst"
  fi
  cp -a "$SOURCE_DIR" "$dst"

  dkms remove -m "$PACKAGE_NAME" -v "$PACKAGE_VERSION" --all >/dev/null 2>&1 || true
  dkms add -m "$PACKAGE_NAME" -v "$PACKAGE_VERSION"
  dkms build -m "$PACKAGE_NAME" -v "$PACKAGE_VERSION" -k "$KERNEL_VERSION"
  dkms install -m "$PACKAGE_NAME" -v "$PACKAGE_VERSION" -k "$KERNEL_VERSION" --force
  depmod -a "$KERNEL_VERSION"

  print_block "build_from_source dkms" "status" "installed" "kernel" "$KERNEL_VERSION" "source" "$dst"
}

load_modules() {
  modprobe cfg80211 || true
  modprobe aic_load_fw
  modprobe aic8800_fdrv
}

main() {
  check_environment
  build_driver
  install_firmware_and_rules
  load_modules
  print_block "build_from_source result" "status" "ok" "driver" "aic8800_fdrv" "next" "物理拔插网卡后执行 scripts/collect_diagnostics.sh"
}

main "$@"
