#!/bin/sh
# Session recorder — spawned by the watcher when a warning goes active.
#
# rtl_fm runs with squelch, so it emits bytes only while the channel is open;
# the chunker downstream therefore receives speech rather than hiss. This script
# loops until the end-epoch file (maintained by the watcher, and extended when
# further warnings arrive) passes, then stops rtl_fm. The resulting EOF makes
# the chunker flush its final chunk and exit cleanly.
#
# Every value comes from the environment the watcher sets. Absolute tool paths
# matter: launchd starts with a bare PATH.

set -u

STORMNET_HOME="${STORMNET_HOME:-$HOME/.local/share/stormnet}"
export STORMNET_HOME

FREQ="${STORMNET_FREQ:?STORMNET_FREQ not set}"
GAIN="${STORMNET_GAIN:-42}"
SQUELCH="${STORMNET_SQUELCH:-50}"
RATE="${STORMNET_SAMPLE_RATE:-24000}"
RTL_FM="${STORMNET_RTL_FM:-rtl_fm}"
PY="${STORMNET_PYTHON:-python3}"

STATE_DIR="$STORMNET_HOME/state"
END_FILE="$STATE_DIR/end_epoch"
RTL_PID_FILE="$STATE_DIR/rtl_fm.pid"

mkdir -p "$STORMNET_HOME/captures" "$STORMNET_HOME/transcripts" "$STATE_DIR"

echo ">> recorder: $FREQ gain $GAIN squelch $SQUELCH until $(cat "$END_FILE" 2>/dev/null || echo '?')" >&2

# Start rtl_fm inside a group so its PID can be captured despite the pipe, and
# stop only that process later. A blanket `pkill rtl_fm` would kill unrelated
# SDR work on the same machine — including the operator's own session.
{
  "$RTL_FM" -f "$FREQ" -M fm -s "$RATE" -F 9 -E deemp \
    -g "$GAIN" -l "$SQUELCH" - &
  echo $! > "$RTL_PID_FILE"
  wait
} | "$PY" -m stormnet.chunker &
PIPE_PID=$!

stop_radio() {
  if [ -f "$RTL_PID_FILE" ]; then
    kill "$(cat "$RTL_PID_FILE")" 2>/dev/null
    rm -f "$RTL_PID_FILE"
  fi
}
trap 'stop_radio' INT TERM

while :; do
  sleep 10
  kill -0 "$PIPE_PID" 2>/dev/null || break        # pipeline died on its own
  END=$(cat "$END_FILE" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  [ "$NOW" -gt "${END%.*}" ] && break             # last warning + grace passed
done

stop_radio           # EOF -> the chunker flushes its remainder and exits
wait "$PIPE_PID" 2>/dev/null
echo ">> recorder: session over" >&2
