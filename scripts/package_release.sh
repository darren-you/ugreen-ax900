#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
TARGET_KERNEL="6.17.0-14-generic"
TARGET_ARCH="x86_64"
DIST="$ROOT/dist"
STAGE="$DIST/ugreen_ax900_prebuilt"
ASSET="$DIST/ugreen_ax900_prebuilt_ubuntu24.04_kernel6.17.0-14_x86_64_v$VERSION.tar.gz"

print_block() {
  local title="$1"
  shift
  printf '%s\n' "$title"
  while [ "$#" -gt 0 ]; do
    printf '  %-12s %s\n' "$1" "$2"
    shift 2
  done
}

rm -rf "$STAGE"
install -d -m 0755 "$STAGE/modules/$TARGET_KERNEL/$TARGET_ARCH" "$STAGE/firmware" "$STAGE/udev" "$STAGE/modules-load" "$STAGE/scripts"

SOURCE_ASSET="$ROOT/release_assets/ugreen_ax900_prebuilt_ubuntu24.04_kernel6.17.0-14_x86_64.tar.gz"
[ -f "$SOURCE_ASSET" ] || {
  print_block "package_release result" "status" "failed" "reason" "缺少 $SOURCE_ASSET"
  exit 1
}

tmp="$(mktemp -d)"
tar -C "$tmp" -xzf "$SOURCE_ASSET"
cp -a "$tmp/prebuilt/modules/$TARGET_KERNEL/$TARGET_ARCH/"*.ko.zst "$STAGE/modules/$TARGET_KERNEL/$TARGET_ARCH/"
cp -a "$tmp/prebuilt/firmware/aic8800D80" "$STAGE/firmware/"
cp -a "$tmp/prebuilt/udev/aic.rules" "$STAGE/udev/aic.rules"
cp -a "$tmp/prebuilt/modules-load/aic8800.conf" "$STAGE/modules-load/aic8800.conf"

install -m 0755 "$ROOT/scripts/install_prebuilt.sh" "$STAGE/scripts/install_prebuilt.sh"
install -m 0755 "$ROOT/scripts/collect_diagnostics.sh" "$STAGE/scripts/collect_diagnostics.sh"
install -m 0644 "$ROOT/VERSION" "$STAGE/VERSION"

release_readme="$STAGE/README.release.md"
cat > "$release_readme" <<'RELEASE_README'
# UGREEN AX900 prebuilt driver package

This package is only for Ubuntu 24.04 x86_64 with kernel `6.17.0-14-generic`.

Install:

```bash
tar -xzf ugreen_ax900_prebuilt_ubuntu24.04_kernel6.17.0-14_x86_64_v__VERSION__.tar.gz
cd ugreen_ax900_prebuilt
sha256sum -c SHA256SUMS
sudo bash scripts/install_prebuilt.sh
```

After installation, physically unplug and replug the adapter. The working USB ID should become `368b:8d85` or another `368b:8d8*` AIC8800D80 variant, and `lsusb -t` should show `Driver=aic8800_fdrv`.
RELEASE_README
sed "s/__VERSION__/$VERSION/g" "$release_readme" > "$release_readme.tmp"
mv "$release_readme.tmp" "$release_readme"

(cd "$STAGE" && sha256sum modules/$TARGET_KERNEL/$TARGET_ARCH/*.ko.zst firmware/aic8800D80/* udev/aic.rules modules-load/aic8800.conf scripts/*.sh VERSION README.release.md > SHA256SUMS)
rm -f "$ASSET"
(cd "$DIST" && tar --owner=0 --group=0 -czf "$(basename "$ASSET")" ugreen_ax900_prebuilt)
(cd "$DIST" && sha256sum "$(basename "$ASSET")" > "$(basename "$ASSET").sha256")

print_block "package_release result" "status" "ok" "asset" "$ASSET" "sha256" "$ASSET.sha256"
