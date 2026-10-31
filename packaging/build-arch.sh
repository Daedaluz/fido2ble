#!/bin/bash
# Build the Arch Linux package in a container. Output lands in dist/.
# Usage: packaging/build-arch.sh
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
runtime=${CONTAINER_RUNTIME:-podman}
image=${ARCH_IMAGE:-docker.io/library/archlinux:latest}
pkgver=$(sed -n 's/^pkgver=//p' "$repo/packaging/arch/PKGBUILD")
out="$repo/dist"
mkdir -p "$out"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp "$repo/packaging/arch/PKGBUILD" "$work/"
git -C "$repo" archive --format=tar.gz --prefix="fido2ble-$pkgver/" -o "$work/fido2ble-$pkgver.tar.gz" HEAD

"$runtime" run --rm \
  -v "$work:/build:z" -v "$out:/out:z" \
  "$image" bash -euo pipefail -c '
    pacman -Syu --noconfirm --needed base-devel python python-build python-installer python-wheel python-setuptools >/dev/null
    useradd -m builder
    chown -R builder /build
    cd /build
    # bluez is a runtime dependency only; -d skips installing it in the container.
    sudo -u builder makepkg -f -d
    cp /build/*.pkg.tar.zst /out/
    ls -l /out
  '
