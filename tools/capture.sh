#!/bin/sh
# Capture a bounded audio sample for offline analysis.
#
#   sh tools/capture.sh 147.180M        # 30 s at the monitor profile
#   sh tools/capture.sh 147.180M 60     # 60 s
#   GAIN=34 sh tools/capture.sh 147.180M 20
#
# Squelch defaults OFF here, unlike listening: a gain ladder or a denoiser has to
# be judged on the hiss as well as the speech, and a gated capture of a quiet net
# is just a file full of silence.
#
# The capture is byte-bounded with `head -c`, so rtl_fm dies of SIGPIPE when the
# quota is met — no timeout, no orphan. Writes <out>/cap_<freq>_<stamp>.raw
# (16-bit signed LE, mono) plus a .wav of the same samples.

set -u

FREQ="${1:?usage: capture.sh <frequency, e.g. 147.180M> [seconds]}"
SECS="${2:-30}"
RATE=24000
BYTES=$((RATE * 2 * SECS))
STAMP=$(date +%Y%m%d_%H%M%S)
OUT_DIR="${STORMNET_CAPTURE_OUT:-captures}"
BASE="$OUT_DIR/cap_${FREQ}_${STAMP}"

RTL_FM="${STORMNET_RTL_FM:-rtl_fm}"
SSH_HOST="${STORMNET_SSH_HOST:-}"
HOME_DIR="${STORMNET_HOME:-$HOME/.local/share/stormnet}"
STATE="$HOME_DIR/state"
FORCE="${FORCE:-}"

if [ -z "$FORCE" ]; then
  if [ -f "$STATE/recorder.pid" ] && kill -0 "$(cat "$STATE/recorder.pid")" 2>/dev/null; then
    echo "!! A storm session is recording on this receiver. FORCE=1 to take over." >&2
    exit 9
  fi
fi

mkdir -p "$OUT_DIR" "$STATE"
touch "$STATE/manual_hold"
trap 'rm -f "$STATE/manual_hold"' EXIT INT TERM

echo ">> capturing $FREQ for ${SECS}s (gain ${GAIN:-42}, squelch ${SQL:-0}) -> $BASE.raw" >&2

RTL_ARGS="-M fm -s 24k -F 9 -E deemp -g ${GAIN:-42} -l ${SQL:-0}"

if [ -n "$SSH_HOST" ]; then
  # shellcheck disable=SC2029
  ssh "$SSH_HOST" "$RTL_FM -f $FREQ $RTL_ARGS - | head -c $BYTES" > "$BASE.raw"
else
  # shellcheck disable=SC2086
  "$RTL_FM" -f "$FREQ" $RTL_ARGS - | head -c "$BYTES" > "$BASE.raw"
fi

sox -t raw -r "$RATE" -e signed-integer -b 16 -c 1 "$BASE.raw" "$BASE.wav"

echo ">> done:" >&2
ls -l "$BASE.raw" "$BASE.wav" >&2
