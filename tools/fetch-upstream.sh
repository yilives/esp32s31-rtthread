#!/usr/bin/env bash
# Clone the upstream RT-Thread checkout this port is pinned to.
#
# The port compiles RT-Thread sources unmodified, so the revision matters: 5.4.0
# at 9e6dec3b is what the self test and both checkers were verified against.
#
#   ./tools/fetch-upstream.sh              # clone into ../rt-thread
#   ./tools/fetch-upstream.sh /opt/rtt     # somewhere else
#
# Uses --depth 1: the port never needs RT-Thread history, and a full clone is
# over 1 GB.

set -euo pipefail

REVISION="${RTT_REVISION:-9e6dec3b14f5bf5514b0da829736c2485c38b0a7}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATH_TO="${1:-$ROOT/rt-thread}"

if [ -f "$PATH_TO/src/thread.c" ]; then
    echo "RT-Thread already present at $PATH_TO"
    have="$(git -C "$PATH_TO" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "  revision: $have"
    if [ "$have" != "$REVISION" ]; then
        echo "  note: pinned revision for this port is $REVISION"
        echo "        a different revision may still work; the port only needs the public API"
    fi
    exit 0
fi

echo "Cloning RT-Thread into $PATH_TO ..."
git clone --depth 1 https://github.com/RT-Thread/rt-thread.git "$PATH_TO"

if git -C "$PATH_TO" fetch --depth 1 origin "$REVISION" 2>/dev/null; then
    git -C "$PATH_TO" checkout --detach "$REVISION"
    echo "Checked out $REVISION (the revision this port was verified against)"
else
    echo "Could not fetch $REVISION; staying on the default branch."
    echo "The port needs RT-Thread 5.4.0 or newer 5.x."
fi

echo
echo "Done. Build with:"
echo "  ./tools/build.sh"
