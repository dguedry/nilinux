#!/usr/bin/env bash
# Build and install the Flatpak for the current user.
set -euo pipefail
cd "$(dirname "$0")"
flatpak-builder --user --install --force-clean --ccache build-dir org.nilinux.NILinux.yml "$@"
echo "Run with: flatpak run org.nilinux.NILinux"
