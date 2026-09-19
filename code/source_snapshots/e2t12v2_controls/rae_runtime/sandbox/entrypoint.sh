#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    echo "ERROR: sandbox must not run as root" >&2
    exit 1
fi

mkdir -p /workspace/strategies /workspace/output /workspace/input
exec "$@"

mkdir -p /workspace/strategies /workspace/output /workspace/output/artifacts /workspace/input