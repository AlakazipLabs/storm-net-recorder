#!/bin/sh
# Listen to a frequency through the SDR, live.
#
#   sh tools/listen.sh 147.180M monitor   # distant repeater / net traffic
#   sh tools/listen.sh 162.475M wx        # NOAA weather broadcast
#   sh tools/listen.sh 462.5625M          # a handheld a few feet away (default)
#   sh tools/listen.sh 89.5M wbfm         # broadcast FM
#
# The profiles exist because one gain/squelch pair does not fit every signal.
# The DEFAULT voice profile is deliberately deaf to distant signals: low gain so
# a strong nearby transmitter doesn't clip the front end, squelch up so hiss
# stays quiet. Point it at a repeater output and you will hear nothing and
# conclude the radio is broken. Use `monitor` for anything you are not
# transmitting yourself. Hiss between transmissions is squelch working, not a
# fault.
#
# Gain and squelch are site-specific — they depend on your antenna, your noise
# floor, and how far away the machine is. The numbers below are a starting point
# measured on one indoor antenna; tools/analyze_gain_ladder.py is how you find
# yours. Override per run: GAIN=<dB> SQL=<level> sh tools/listen.sh ...
#
# By default rtl_fm runs on this machine. If your SDR lives on another box, set
# STORMNET_SSH_HOST=user@host and the capture half runs there while audio plays
# here. Requires: sox (local playback), librtlsdr (wherever the dongle is).

set -u

FREQ="${1:?usage: listen.sh <frequency, e.g. 147.180M> [wx|monitor|voice|wbfm]}"
MODE="${2:-voice}"
GAIN="${GAIN:-}"
SQL="${SQL:-}"
FORCE="${FORCE:-}"

RTL_FM="${STORMNET_RTL_FM:-rtl_fm}"
SSH_HOST="${STORMNET_SSH_HOST:-}"
HOME_DIR="${STORMNET_HOME:-$HOME/.local/share/stormnet}"
STATE="$HOME_DIR/state"

# --- collision guard -------------------------------------------------------
# One dongle, two claimants. If the watcher is recording a storm net, taking the
# receiver would kill that recording and its transcript. Refuse unless forced.
# Either way, drop a manual_hold marker: while it is fresh the watcher DEFERS
# rather than seizing the receiver back mid-listen.
guard() {
  if [ -n "$FORCE" ]; then return 0; fi
  if [ -f "$STATE/recorder.pid" ] && kill -0 "$(cat "$STATE/recorder.pid")" 2>/dev/null; then
    echo "!! A storm session is RECORDING on this receiver right now." >&2
    echo "!! Listening would kill the recording and its transcript. FORCE=1 to override." >&2
    exit 9
  fi
  if [ "$(cat "$STATE/end_epoch" 2>/dev/null || echo 0)" -gt "$(date +%s)" ]; then
    echo "!! A warning window is still open; the watcher expects the receiver." >&2
    echo "!! FORCE=1 to take it anyway." >&2
    exit 9
  fi
}

case "$MODE" in
  wbfm)  ARGS="-M wbfm";                                        RATE=32000 ;;
  wx)    ARGS="-M fm -s 24k -F 9 -E deemp ${GAIN:+-g $GAIN} -l ${SQL:-0}";   RATE=24000 ;;
  monitor) ARGS="-M fm -s 24k -F 9 -E deemp -g ${GAIN:-42} -l ${SQL:-50}";   RATE=24000 ;;
  *)     ARGS="-M fm -s 24k -g ${GAIN:-30} -l ${SQL:-50}";      RATE=24000 ;;
esac

guard
mkdir -p "$STATE"
touch "$STATE/manual_hold"
trap 'rm -f "$STATE/manual_hold"' EXIT INT TERM

echo ">> tuning $FREQ (profile: $MODE) — Ctrl-C to stop" >&2

# rtl_fm's stderr is left visible on purpose: "Found 1 device(s) / Tuned to ..."
# is the success signal, and "usb_claim_interface error -3" is how you learn
# another process already holds the dongle. Hiding it turns both into silence.
if [ -n "$SSH_HOST" ]; then
  # shellcheck disable=SC2029  # deliberate client-side expansion
  ssh "$SSH_HOST" "$RTL_FM -f $FREQ $ARGS -" \
    | play -q -t raw -r "$RATE" -e signed-integer -b 16 -c 1 -
else
  # shellcheck disable=SC2086  # ARGS is a deliberate word-split argument list
  "$RTL_FM" -f "$FREQ" $ARGS - \
    | play -q -t raw -r "$RATE" -e signed-integer -b 16 -c 1 -
fi

st=$?
[ "$st" -ne 0 ] && echo ">> pipeline exited $st — check the rtl_fm messages above (device busy? bad frequency?)" >&2
exit "$st"
