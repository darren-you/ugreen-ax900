#!/usr/bin/env bash
set -euo pipefail

TARGET_KERNEL="6.17.0-14-generic"
TARGET_ARCH="x86_64"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$ROOT/modules/$TARGET_KERNEL/$TARGET_ARCH"
FIRMWARE_DIR="$ROOT/firmware/aic8800D80"
UDEV_RULE="$ROOT/udev/aic.rules"
MODULES_LOAD="$ROOT/modules-load/aic8800.conf"

usage() {
  cat <<EOF
用法: install_prebuilt.sh [--help]

说明:
  安装 UGREEN AX900 / AIC8800D80 预编译驱动包。
  该脚本会写入内核模块、固件、udev 规则和 modules-load 配置，必须使用 sudo 执行。

硬性边界:
  kernel: $TARGET_KERNEL
  arch:   $TARGET_ARCH
EOF
}

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
  print_block "install_prebuilt result" "status" "failed" "reason" "$1"
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "请使用 sudo 执行"
}

check_secure_boot() {
  if command -v mokutil >/dev/null 2>&1; then
    if mokutil --sb-state 2>/dev/null | grep -qi "enabled"; then
      fail "Secure Boot 已开启；预编译模块不会使用本机 MOK 签名，请改用源码构建并按本机策略签名"
    fi
  fi
}

verify_checksums() {
  if [ -f "$ROOT/SHA256SUMS" ]; then
    (cd "$ROOT" && sha256sum -c SHA256SUMS >/tmp/ugreen_ax900_sha256.log) ||
      fail "SHA256 校验失败，详情见 /tmp/ugreen_ax900_sha256.log"
  fi
}

install_files() {
  local backup_dir="/var/backups/ugreen_ax900/$(date +%Y%m%d%H%M%S)"
  local kernel_module_dir="/lib/modules/$TARGET_KERNEL/updates/dkms"

  install -d -m 0755 "$backup_dir" "$kernel_module_dir" /lib/firmware /usr/lib/udev/rules.d /etc/modules-load.d

  for name in aic8800_fdrv.ko.zst aic_load_fw.ko.zst; do
    if [ -f "$kernel_module_dir/$name" ]; then
      cp -a "$kernel_module_dir/$name" "$backup_dir/$name"
    fi
    install -m 0644 "$MODULE_DIR/$name" "$kernel_module_dir/$name"
  done

  if [ -d /lib/firmware/aic8800D80 ]; then
    cp -a /lib/firmware/aic8800D80 "$backup_dir/aic8800D80"
  fi
  rm -rf /lib/firmware/aic8800D80
  cp -a "$FIRMWARE_DIR" /lib/firmware/aic8800D80

  if [ -f /usr/lib/udev/rules.d/aic.rules ]; then
    cp -a /usr/lib/udev/rules.d/aic.rules "$backup_dir/aic.rules"
  fi
  install -m 0644 "$UDEV_RULE" /usr/lib/udev/rules.d/aic.rules
  install -m 0644 "$MODULES_LOAD" /etc/modules-load.d/aic8800.conf

  depmod -a "$TARGET_KERNEL"
  udevadm control --reload || true

  print_block "install_prebuilt files" "status" "installed" "backup" "$backup_dir"
}

load_modules() {
  modprobe -r aic8800_fdrv 2>/dev/null || true
  modprobe -r aic_load_fw 2>/dev/null || true
  modprobe cfg80211 || true
  modprobe aic_load_fw
  modprobe aic8800_fdrv
}

try_storage_switch() {
  if lsusb | grep -qi "a69c:5723"; then
    if command -v usb_modeswitch >/dev/null 2>&1; then
      usb_modeswitch -KQ -v a69c -p 5723 || true
    else
      print_block "install_prebuilt warning" "status" "storage_mode" "next" "安装 usb-modeswitch 后重新拔插网卡"
    fi
  fi
}

main() {
  case "${1:-}" in
    -h|--help)
      usage
      exit 0
      ;;
    "")
      ;;
    *)
      fail "未知参数: $1"
      ;;
  esac

  require_root
  [ "$(uname -r)" = "$TARGET_KERNEL" ] || fail "预编译包只适配 $TARGET_KERNEL，当前为 $(uname -r)"
  [ "$(uname -m)" = "$TARGET_ARCH" ] || fail "预编译包只适配 $TARGET_ARCH，当前为 $(uname -m)"
  [ -f "$MODULE_DIR/aic8800_fdrv.ko.zst" ] || fail "缺少 aic8800_fdrv.ko.zst"
  [ -f "$MODULE_DIR/aic_load_fw.ko.zst" ] || fail "缺少 aic_load_fw.ko.zst"
  [ -d "$FIRMWARE_DIR" ] || fail "缺少 firmware/aic8800D80"
  check_secure_boot
  verify_checksums
  install_files
  load_modules
  try_storage_switch
  print_block "install_prebuilt result" "status" "ok" "driver" "aic8800_fdrv" "next" "物理拔插网卡后执行 scripts/collect_diagnostics.sh"
}

main "$@"
