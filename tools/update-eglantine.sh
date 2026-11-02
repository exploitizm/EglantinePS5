#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# make update: moves the Eglantine submodule to the newest commit of its ps5 branch, lists what
# came in, and checks that the text anchors of the port (tools/prepare-vulkan-port.py) still match.
# It stages the submodule's new commit and nothing else; commit that yourself once `make test`
# passes.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
git submodule update --init Eglantine
# The patches (tools/eglantine-patches) come out first, so the submodule moves from a clean tree.
bash tools/apply-eglantine-patches.sh --revert
before=$(git -C Eglantine rev-parse HEAD)
git submodule update --remote Eglantine
after=$(git -C Eglantine rev-parse HEAD)
# Staged, so that make deps (git submodule update --init) keeps this commit instead of going back to
# the one recorded before; committing is left to you.
git add Eglantine
if [[ $before == "$after" ]]; then
    echo "Eglantine is already at the newest commit ($(git -C Eglantine rev-parse --short HEAD))."
else
    echo "Eglantine ${before:0:10} -> ${after:0:10}:"
    git -C Eglantine log --oneline "$before..$after" | head -40
    echo "($(git -C Eglantine rev-list --count "$before..$after") commits, $(git -C Eglantine diff --shortstat "$before" "$after"))"
fi
# Put them back on the new commit: if one no longer applies, say so now.
bash tools/apply-eglantine-patches.sh
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
if ! python3 -I tools/prepare-vulkan-port.py --report Eglantine "$scratch" >/dev/null; then
    echo "The port needs adapting to the new source (above). The new commit is staged;" >&2
    echo "'git restore --staged Eglantine && git submodule update Eglantine' goes back." >&2
    exit 1
fi
echo "The Vulkan port still applies. Next: make test, then commit the submodule."
