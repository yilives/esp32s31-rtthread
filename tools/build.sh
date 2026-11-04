#!/usr/bin/env bash
# Build / flash on Linux, macOS and WSL.
#
# This repository does not contain ESP-IDF; it contains the RT-Thread component,
# a patch for ESP-IDF's own components, and an application. So IDF_PATH has to
# point at an ESP-IDF tree that has been patched and has the component installed:
#
#   export IDF_PATH=/opt/esp/esp-idf
#   ./tools/build.sh            # build
#   ./tools/build.sh flash -p /dev/ttyUSB0
#
# Prerequisite: ESP-IDF's own environment already sourced, or IDF_PATH set and
# idf.py reachable. That is deliberate - on Linux the IDF export.sh works
# correctly, and reimplementing it here would only diverge from it.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${IDF_PATH:-}" ]; then
    echo "ERROR: IDF_PATH is not set." >&2
    echo "This repository does not ship ESP-IDF. Example:" >&2
    echo "  export IDF_PATH=/opt/esp/esp-idf" >&2
    echo "  . \$IDF_PATH/export.sh" >&2
    exit 1
fi

if [ ! -f "$IDF_PATH/tools/idf.py" ]; then
    echo "ERROR: $IDF_PATH does not look like ESP-IDF (no tools/idf.py)" >&2
    exit 1
fi

if [ ! -f "$IDF_PATH/components/rtthread/CMakeLists.txt" ]; then
    echo "ERROR: the RT-Thread component is not installed in $IDF_PATH" >&2
    echo "Copy component/ into \$IDF_PATH/components/rtthread/ first." >&2
    exit 1
fi

if [ ! -f "$here/project/CMakeLists.txt" ]; then
    echo "ERROR: project/ not found under $here" >&2
    exit 1
fi

# project/CMakeLists.txt defaults RTTHREAD_ROOT to ../rt-thread.
if [ -f "$here/rt-thread/src/thread.c" ]; then
    export RTTHREAD_ROOT="${RTTHREAD_ROOT:-$here/rt-thread}"
elif [ -n "${RTTHREAD_ROOT:-}" ] && [ -f "${RTTHREAD_ROOT}/src/thread.c" ]; then
    :
else
    echo "ERROR: upstream RT-Thread not found at $here/rt-thread" >&2
    echo "Run: ./tools/fetch-upstream.sh" >&2
    exit 1
fi

IDF_PY="idf.py"
if ! command -v idf.py >/dev/null 2>&1; then
    IDF_PY="$IDF_PATH/tools/idf.py"
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

cd "$here/project"

if [ "$#" -eq 0 ]; then
    set -- build
fi

exec "$IDF_PY" -DIDF_TARGET=esp32s31 "$@"
