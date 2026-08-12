"""The watcher — poll NWS, start and stop the recorder, notify.

Every poll_secs: fetch active alerts for the configured point. On a NEW warning
whose event is in trigger_events:

  1. Notify (event, expiry, where the transcript will land).
  2. Start the recorder if it isn't running; extend its end time if it is.

When the last warning expires the recorder self-stops — it watches the end-epoch
file rather than being killed — and one all-clear notification points at the
transcript.

The deferral rule is the important one. If you are already listening on the
dongle by hand, the watcher does NOT take it from you. It writes the end time,
tells you once that it is not recording, and starts as soon as your session
ends. A monitoring tool that kills your radio during a tornado warning is worse
than no monitoring tool.

Threat model: one outbound HTTPS GET to api.weather.gov. The response is parsed
as JSON and never shelled out. Alert text is truncated before it reaches any
notifier. No credentials exist in this process.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .notify import Notifier

log = logging.getLogger("stormnet.watch")


class Watcher:
    def __init__(self, cfg, notifier=None):
        self.cfg = cfg
        self.notifier = notifier if notifier is not None else Notifier(cfg)
        self._stop = False
        self._recorder: subprocess.Popen | None = None

    # --- NWS ------------------------------------------------------------
    def fetch_alerts(self, opener=urllib.request.urlopen) -> dict:
        req = urllib.request.Request(
            self.cfg.alerts_url,
            headers={"User-Agent": self.cfg.user_agent,
                     "Accept": "application/geo+json"},
        )
        with opener(req, timeout=self.cfg.http_timeout) as resp:
            return json.load(resp)

    def parse_warnings(self, payload: dict, now: float) -> list[dict]:
        """Pure: NWS FeatureCollection -> the active warnings we care about."""
        out = []
        for feature in payload.get("features", []):
            props = feature.get("properties") or {}
            event = props.get("event")
            if event not in self.cfg.trigger_events:
                continue
            ends_iso = props.get("ends") or props.get("expires")
            try:
                ends = datetime.fromisoformat(ends_iso).timestamp()
            except (TypeError, ValueError):
                log.warning("unparseable ends/expires %r on %s", ends_iso, event)
                continue
            if ends <= now:
                continue
            alert_id = props.get("id") or feature.get("id")
            if not alert_id:
                continue
            out.append({
                "id": alert_id,
                "event": event,
                "headline": props.get("headline") or event,
                "ends": ends,
            })
        return out

    # --- state ----------------------------------------------------------
    def load_state(self) -> dict:
        try:
            return json.loads(self.cfg.state_file.read_text())
        except (OSError, ValueError):
            return {"notified": {}, "session_active": False}

    def save_state(self, state: dict) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.cfg.state_file)

    # --- recorder -------------------------------------------------------
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def manual_hold_active(self, now: float) -> bool:
        """True while a manual SDR session holds the dongle (bounded by hold_max_secs)."""
        try:
            age = now - self.cfg.hold_file.stat().st_mtime
        except OSError:
            return False
        return age < self.cfg.hold_max_secs

    def recorder_alive(self) -> bool:
        if self._recorder is not None:
            if self._recorder.poll() is None:
                return True
            self._recorder = None  # reap
        try:
            pid = int(self.cfg.pid_file.read_text().strip())
        except (OSError, ValueError):
            return False
        return self._pid_alive(pid)

    def ensure_recorder(self, spawn=subprocess.Popen) -> bool:
        """Start the recorder if it isn't running. Returns True if spawned."""
        if self.recorder_alive():
            return False
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        logf = open(self.cfg.home / "recorder.log", "ab")
        env = {
            **os.environ,
            "STORMNET_HOME": str(self.cfg.home),
            "STORMNET_FREQ": self.cfg.freq,
            "STORMNET_GAIN": self.cfg.gain,
            "STORMNET_SQUELCH": self.cfg.squelch,
            "STORMNET_SAMPLE_RATE": str(self.cfg.sample_rate),
            "STORMNET_RTL_FM": self.cfg.rtl_fm,
            "STORMNET_PYTHON": self.cfg.python,
        }
        self._recorder = spawn(
            ["/bin/sh", str(self.cfg.record_script)],
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env=env,
        )
        self.cfg.pid_file.write_text(str(self._recorder.pid))
        log.info("recorder spawned, pid %d", self._recorder.pid)
        return True

    def transcript_path(self, now: float) -> Path:
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        return self.cfg.transcript_dir / f"stormnet_{day}.txt"

    # --- the loop -------------------------------------------------------
    def poll_once(self, now: float | None = None, fetch=None) -> None:
        now = time.time() if now is None else now
        fetch = fetch or self.fetch_alerts
        warnings = self.parse_warnings(fetch(), now)
        state = self.load_state()
        notified = state.get("notified", {})

        for w in warnings:
            if w["id"] in notified:
                continue
            level = self.cfg.trigger_events[w["event"]]
            ends_local = datetime.fromtimestamp(w["ends"]).strftime("%H:%M")
            self.notifier.send(
                level=level,
                title=f"{w['event']} — recording started",
                text=(f"{w['headline']}\n"
                      f"Until {ends_local}. Recording and transcribing "
                      f"{self.cfg.freq}.\n"
                      f"Transcript: {self.transcript_path(now)}"),
                dedup_key=f"stormnet:{w['id']}",
            )
            notified[w["id"]] = w["ends"]
            log.info("alerted on %s (%s)", w["event"], w["id"])

        if warnings:
            end_epoch = max(w["ends"] for w in warnings) + self.cfg.grace_secs
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            self.cfg.end_file.write_text(str(int(end_epoch)))
            if self.manual_hold_active(now):
                if not state.get("hold_notified"):
                    self.notifier.send(
                        level="info",
                        title="Recording deferred",
                        text=("A warning is active, but a manual SDR session holds "
                              "the receiver — NOT recording. Recording starts when "
                              "your session ends (the hold expires on its own after "
                              "an hour)."),
                        dedup_key=f"stormnet:hold:{int(now)}",
                    )
                    state["hold_notified"] = True
            else:
                self.ensure_recorder()
                state["hold_notified"] = False
            state["session_active"] = True
        elif state.get("session_active") and not self.recorder_alive():
            self.notifier.send(
                level="info",
                title="All clear",
                text=(f"Warnings expired; recording stopped.\n"
                      f"Transcript: {self.transcript_path(now)}"),
                dedup_key=f"stormnet:allclear:{int(now)}",
            )
            state["session_active"] = False

        # prune day-old ids so the state file can't grow without bound
        state["notified"] = {k: v for k, v in notified.items() if v > now - 86400}
        self.save_state(state)

    def _handle_signal(self, signum, _frame):
        log.info("signal %s received, shutting down", signum)
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        for d in (self.cfg.capture_dir, self.cfg.transcript_dir, self.cfg.state_dir):
            d.mkdir(parents=True, exist_ok=True)
        log.info("watching %s,%s every %ds -> %s",
                 self.cfg.lat, self.cfg.lon, self.cfg.poll_secs, self.cfg.freq)
        while not self._stop:
            try:
                self.poll_once()
            except Exception:
                log.exception("poll failed; retrying next cycle")
            for _ in range(self.cfg.poll_secs):
                if self._stop:
                    break
                time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stormnet-watch",
        description="Watch NWS alerts and record/transcribe the local storm net.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--check", action="store_true",
                        help="validate config and exit without watching")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    if args.check:
        # Everything a live session needs, proven now rather than mid-warning.
        problems = []
        if not cfg.record_script.is_file():
            problems.append(f"recorder script missing: {cfg.record_script}")
        model = Path(cfg.whisper_model).expanduser()
        if not model.is_file():
            problems.append(f"whisper model not found: {model}")

        print(f"watching {cfg.lat},{cfg.lon} on {cfg.freq}")
        print(f"    events:    {', '.join(sorted(cfg.trigger_events))}")
        print(f"    notify:    {cfg.notify_mode}")
        print(f"    home:      {cfg.home}")
        print(f"    rtl_fm:    {cfg.rtl_fm}")
        print(f"    ffmpeg:    {cfg.ffmpeg}")
        print(f"    whisper:   {cfg.whisper_bin}")
        print(f"    model:     {model}")
        print(f"    recorder:  {cfg.record_script}")
        if problems:
            print("\nNOT READY:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("\nOK — ready to record.")
        return 0

    Watcher(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
