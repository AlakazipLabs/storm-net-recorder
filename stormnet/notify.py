"""Notification dispatch — four modes, no service-specific code.

The recorder does not know what Telegram is. It produces an alert object and
hands it to whichever transport the config names:

  none     log it and move on
  command  exec argv with the alert JSON on stdin (no shell, ever)
  webhook  HTTPS POST of the alert JSON
  spool    atomically write one JSON file into a directory, for a separate
           process to pick up and delete

`spool` exists because the useful pattern is decoupling: the thing holding your
bot credentials stays a separate process, and this daemon never sees a token.

Alert JSON:
  {"source", "level", "title", "text", "ts", "dedup_key"}

A notification failure is logged and swallowed. Losing the alert is bad;
crashing the watcher during a tornado warning is worse.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("stormnet.notify")

LEVELS = frozenset({"info", "warn", "crit"})
COMMAND_TIMEOUT = 30
WEBHOOK_TIMEOUT = 15


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Notifier:
    def __init__(self, cfg, clock: Callable[[], float] = time.time,
                 now_iso: Callable[[], str] = _now_iso):
        self.cfg = cfg
        self.clock = clock
        self.now_iso = now_iso
        self._seq = 0
        if cfg.notify_mode == "spool":
            self._spool_dir = Path(cfg.notify_spool_dir).expanduser()
            self._spool_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._spool_dir, 0o700)

    def build(self, level: str, title: str, text: str, dedup_key: str) -> dict:
        if level not in LEVELS:
            raise ValueError(f"bad level {level!r} (must be one of {sorted(LEVELS)})")
        return {
            "source": self.cfg.notify_source,
            "level": level,
            "title": title,
            "text": text[: self.cfg.notify_text_max],
            "ts": self.now_iso(),
            "dedup_key": dedup_key,
        }

    def send(self, level: str, title: str, text: str, dedup_key: str) -> bool:
        """Dispatch one alert. Returns True if the transport accepted it."""
        alert = self.build(level, title, text, dedup_key)
        mode = self.cfg.notify_mode
        try:
            if mode == "none":
                log.info("[%s] %s — %s", level, title, text.replace("\n", " ")[:200])
                return True
            if mode == "command":
                return self._send_command(alert)
            if mode == "webhook":
                return self._send_webhook(alert)
            if mode == "spool":
                return self._send_spool(alert) is not None
        except Exception:
            # Never let a broken notifier take down the watcher.
            log.exception("notification failed (%s): %s", mode, dedup_key)
            return False
        log.error("unknown notify mode %r", mode)
        return False

    def _send_command(self, alert: dict, run=None) -> bool:
        # Resolved at call time, not as a default argument: a default binds at
        # definition time, which silently defeats patching subprocess.run.
        run = run or subprocess.run
        argv = self.cfg.notify_command
        if isinstance(argv, str):
            # A bare string would need a shell to split; refuse rather than
            # invent quoting rules over user-supplied alert text.
            raise ValueError("[notify].command must be a list of arguments, not a string")
        result = run(list(argv), input=json.dumps(alert), text=True,
                     capture_output=True, timeout=COMMAND_TIMEOUT)
        if result.returncode != 0:
            log.error("notify command exited %d: %s",
                      result.returncode, result.stderr.strip()[:300])
            return False
        return True

    def _send_webhook(self, alert: dict, opener=None) -> bool:
        # Call-time resolution, same reason as _send_command.
        opener = opener or urllib.request.urlopen
        req = urllib.request.Request(
            self.cfg.notify_url,
            data=json.dumps(alert).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": self.cfg.user_agent},
            method="POST",
        )
        with opener(req, timeout=WEBHOOK_TIMEOUT) as resp:
            status = getattr(resp, "status", 0)
        if not 200 <= status < 300:
            log.error("webhook returned HTTP %s", status)
            return False
        return True

    def _send_spool(self, alert: dict) -> Path | None:
        """Atomic drop: unique temp name, fsync, rename. Lexical sort == FIFO."""
        self._seq += 1
        name = f"{int(self.clock() * 1000):013d}_{self._seq:04d}.json"
        final = self._spool_dir / name
        tmp = self._spool_dir / (name + ".tmp")
        data = json.dumps(alert).encode("utf-8")

        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, final)  # atomic within one filesystem
        log.info("spooled %s -> %s", alert["dedup_key"], final.name)
        return final
