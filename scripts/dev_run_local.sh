#!/usr/bin/env bash
# Local dev harness: 3 real VLC instances + the blaufilter controller.
# Usage: ./scripts/dev_run_local.sh <video-file>
# Then open http://localhost:8080
#
# Inject drift manually to watch a correction happen:
#   printf 'seek 42\n' | nc 127.0.0.1 5502
set -euo pipefail

VIDEO="${1:?usage: $0 <video-file>}"

killall vlc 2>/dev/null || true
sleep 0.5

for port in 5501 5502 5503; do
    vlc --extraintf lua --rc-host "127.0.0.1:$port" --loop --no-osd "$VIDEO" &> /dev/null &
done

echo "Waiting for VLC instances..."
sleep 2

BLAUFILTER_HOSTS=127.0.0.1:5501,127.0.0.1:5502,127.0.0.1:5503 \
    blaufilter-controller --web-port 8080
