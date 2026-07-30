#!/usr/bin/env bash
#
# Builds a fully unattended install ISO: takes a stock Rocky Linux 9
# boot/DVD ISO, bakes in this repo's appliance source + ks.cfg, and
# rewrites the boot menu to auto-load the kickstart.
#
# Requirements (install these on your OWN build machine):
#   sudo dnf install -y xorriso syslinux genisoimage
#
# Usage:
#   ./build-iso.sh /path/to/Rocky-9-x86_64-minimal.iso [output.iso]

set -euo pipefail

if ! command -v xorriso &>/dev/null; then
  echo "ERROR: xorriso not found. Install it first:" >&2
  echo "  sudo dnf install -y xorriso syslinux genisoimage   # RHEL/Rocky/Fedora" >&2
  echo "  sudo apt install -y xorriso syslinux-utils genisoimage  # Debian/Ubuntu" >&2
  exit 1
fi

BASE_ISO="${1:?Usage: build-iso.sh /path/to/Rocky-9-x86_64-minimal.iso [output.iso]}"
OUT_ISO="${2:-acme-appliance-rocky9.iso}"

if [ ! -f "$BASE_ISO" ]; then
  echo "ERROR: base ISO not found at $BASE_ISO" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$(mktemp -d)"
MOUNT_DIR="$WORK_DIR/mnt"
EXTRACT_DIR="$WORK_DIR/extract"

cleanup() {
  mountpoint -q "$MOUNT_DIR" 2>/dev/null && umount "$MOUNT_DIR" || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo "==> Mounting base ISO..."
mkdir -p "$MOUNT_DIR" "$EXTRACT_DIR"
mount -o loop,ro "$BASE_ISO" "$MOUNT_DIR"

echo "==> Extracting ISO contents..."
cp -a "$MOUNT_DIR"/. "$EXTRACT_DIR"/
chmod -R u+w "$EXTRACT_DIR"
umount "$MOUNT_DIR"

echo "==> Staging appliance source + kickstart onto the ISO..."
mkdir -p "$EXTRACT_DIR/acme-appliance-src"
find "$REPO_ROOT" -mindepth 1 -maxdepth 1 ! -name .git ! -name venv \
  -exec cp -r {} "$EXTRACT_DIR/acme-appliance-src/" \;
cp "$SCRIPT_DIR/ks.cfg" "$EXTRACT_DIR/ks.cfg"

echo "==> Patching boot menus to auto-load the kickstart..."
if [ -f "$EXTRACT_DIR/isolinux/isolinux.cfg" ]; then
  sed -i 's|append |append inst.ks=cdrom:/ks.cfg |' "$EXTRACT_DIR/isolinux/isolinux.cfg" || true
fi
for grubcfg in "$EXTRACT_DIR/EFI/BOOT/grub.cfg" "$EXTRACT_DIR/boot/grub2/grub.cfg"; do
  if [ -f "$grubcfg" ]; then
    sed -i 's|linuxefi \(.*\)|linuxefi \1 inst.ks=cdrom:/ks.cfg|' "$grubcfg" || true
    sed -i 's|linux \(.*vmlinuz.*\)|linux \1 inst.ks=cdrom:/ks.cfg|' "$grubcfg" || true
  fi
done

echo "==> Rebuilding ISO with xorriso (hybrid BIOS+UEFI boot)..."
VOLID=$(blkid -o value -s LABEL "$BASE_ISO" 2>/dev/null || echo "ACME_APPLIANCE")

xorriso -as mkisofs \
  -o "$OUT_ISO" \
  -V "$VOLID" \
  -J -R -l \
  -isohybrid-mbr "$EXTRACT_DIR/isolinux/isohdpfx.bin" \
  -c isolinux/boot.cat \
  -b isolinux/isolinux.bin -no-emul-boot -boot-load-size 4 -boot-info-table \
  -eltorito-alt-boot \
  -e images/efiboot.img -no-emul-boot -isohybrid-gpt-basdat \
  "$EXTRACT_DIR" 2>&1 | tail -20 || {
    echo "xorriso with the exact boot-catalog flags above failed -- falling back to a simpler (BIOS-only) rebuild:" >&2
    xorriso -as mkisofs -o "$OUT_ISO" -V "$VOLID" -J -R -l \
      -b isolinux/isolinux.bin -no-emul-boot -boot-load-size 4 -boot-info-table \
      -c isolinux/boot.cat "$EXTRACT_DIR"
  }

echo ""
echo "==> Done: $OUT_ISO"
echo "    Boot a VM (or write to USB with 'dd') from this ISO and it will"
echo "    install Rocky Linux 9 and stand up the full appliance with no"
echo "    further interaction. Console output/errors land in /root/ks-post.log"
echo "    on the installed box if you need to debug the %post step."
