#!/bin/bash
# Build the Debian/Ubuntu package for one distribution in a container.
# Usage: packaging/build-deb.sh <image> <suite>
#   e.g. packaging/build-deb.sh docker.io/library/debian:trixie trixie
#        packaging/build-deb.sh docker.io/library/ubuntu:resolute resolute
# The version gets a "~<suite>" suffix so packages for different releases can
# sit side by side. Output lands in dist/.
set -euo pipefail

image=${1:?container image}
suite=${2:?suite name}
repo=$(cd "$(dirname "$0")/.." && pwd)
runtime=${CONTAINER_RUNTIME:-podman}
out="$repo/dist"
mkdir -p "$out"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$repo" archive --format=tar HEAD | tar -x -C "$work"

"$runtime" run --rm \
  -v "$work:/build/fido2ble:z" -v "$out:/out:z" \
  -e SUITE="$suite" \
  "$image" bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
      build-essential debhelper dh-python pybuild-plugin-pyproject \
      python3-all python3-setuptools devscripts fakeroot >/dev/null
    cd /build/fido2ble
    version=$(dpkg-parsechangelog -S Version)
    dch --local "~$SUITE" --distribution "$SUITE" "Build for $SUITE."
    dpkg-buildpackage -us -uc -b
    cp ../*.deb ../*.buildinfo ../*.changes /out/
    ls -l /out
  '
