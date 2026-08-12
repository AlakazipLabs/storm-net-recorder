"""Configuration — every site-specific fact lives in a TOML file, not in code.

Search order (first hit wins):
  1. $STORMNET_CONFIG
  2. ./config.toml
  3. ~/.config/stormnet/config.toml

Runtime state (captures, transcripts, pid/end files) lives under `home`, which
should be on local disk: launchd and cloud-synced folders fight, and captures
get large.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

CONFIG_ENV = "STORMNET_CONFIG"
DEFAULT_LOCATIONS = (Path("config.toml"), Path.home() / ".config/stormnet/config.toml")

VALID_NOTIFY_MODES = ("none", "command", "webhook", "spool")


class ConfigError(RuntimeError):
    """Configuration is missing or unusable. Always actionable in one line."""


def find_config_path(env=os.environ) -> Path:
    explicit = env.get(CONFIG_ENV)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"{CONFIG_ENV} points at {path}, which does not exist")
        return path
    for candidate in DEFAULT_LOCATIONS:
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "No config found. Copy config.example.toml to ./config.toml and edit it, "
        f"or set {CONFIG_ENV} to its path."
    )


def _require(table: dict, section: str, key: str):
    try:
        return table[section][key]
    except KeyError:
        raise ConfigError(f"config is missing [{section}].{key}")


def _resolve_tool(configured: str | None, name: str) -> str:
    """Prefer an explicit path; otherwise find it on PATH. Never guess a prefix.

    Hardcoding /opt/homebrew/bin works on exactly one machine. launchd runs with
    a bare PATH, so an absolute path still has to be resolvable at load time.
    """
    if configured:
        if not Path(configured).is_file():
            raise ConfigError(f"configured {name} path does not exist: {configured}")
        return configured
    found = shutil.which(name)
    if not found:
        raise ConfigError(
            f"{name!r} not found on PATH. Install it, or set its full path in config.toml."
        )
    return found


class Config:
    """Parsed configuration. Attribute access only — no dict spelunking downstream."""

    def __init__(self, data: dict, resolve_tools: bool = True):
        self.lat = float(_require(data, "location", "latitude"))
        self.lon = float(_require(data, "location", "longitude"))

        nws = data.get("nws", {})
        contact = _require(data, "nws", "contact")
        if "@" not in contact:
            raise ConfigError(
                "[nws].contact must be an email address — api.weather.gov requires "
                "a contact in the User-Agent and rate-limits anonymous callers."
            )
        self.contact = contact
        self.user_agent = f"(storm-net-recorder, {contact})"
        self.alerts_url = (
            f"https://api.weather.gov/alerts/active?point={self.lat},{self.lon}"
        )
        self.poll_secs = int(nws.get("poll_secs", 120))
        self.grace_secs = int(nws.get("grace_secs", 120))
        self.http_timeout = int(nws.get("http_timeout", 15))
        # event name -> severity label handed to the notifier
        self.trigger_events = dict(nws.get("trigger_events", {
            "Severe Thunderstorm Warning": "warn",
            "Tornado Warning": "crit",
        }))
        if not self.trigger_events:
            raise ConfigError("[nws].trigger_events is empty — nothing would ever record")

        sdr = data.get("sdr", {})
        self.freq = str(_require(data, "sdr", "frequency"))
        self.gain = str(sdr.get("gain", "42"))
        self.squelch = str(sdr.get("squelch", "50"))
        self.sample_rate = int(sdr.get("sample_rate", 24000))

        audio = data.get("audio", {})
        self.chunk_secs = int(audio.get("chunk_secs", 90))
        self.max_wall_secs = int(audio.get("max_wall_secs", 180))
        self.min_chunk_secs = int(audio.get("min_chunk_secs", 2))
        self.chunk_bytes = self.sample_rate * 2 * self.chunk_secs
        self.min_chunk_bytes = self.sample_rate * 2 * self.min_chunk_secs

        whisper = data.get("whisper", {})
        self.whisper_model = str(_require(data, "whisper", "model"))
        self.whisper_timeout = int(whisper.get("timeout", 300))
        # Seeding the decoder with local callsigns and place names is the single
        # biggest accuracy win on net audio; it is also entirely site-specific.
        self.vocab_prompt = str(whisper.get("vocab_prompt", ""))

        paths = data.get("paths", {})
        self.home = Path(
            os.environ.get("STORMNET_HOME", paths.get("home", "~/.local/share/stormnet"))
        ).expanduser()
        self._record_script_override = paths.get("record_script")

        notify = data.get("notify", {})
        self.notify_mode = str(notify.get("mode", "none"))
        if self.notify_mode not in VALID_NOTIFY_MODES:
            raise ConfigError(
                f"[notify].mode must be one of {', '.join(VALID_NOTIFY_MODES)}, "
                f"got {self.notify_mode!r}"
            )
        self.notify_command = notify.get("command")
        self.notify_url = notify.get("url")
        self.notify_spool_dir = notify.get("spool_dir")
        self.notify_source = str(notify.get("source", "storm-net-recorder"))
        self.notify_text_max = int(notify.get("text_max", 3500))
        self._validate_notify()

        tools = data.get("tools", {})
        if resolve_tools:
            self.rtl_fm = _resolve_tool(tools.get("rtl_fm"), "rtl_fm")
            self.ffmpeg = _resolve_tool(tools.get("ffmpeg"), "ffmpeg")
            self.whisper_bin = _resolve_tool(tools.get("whisper_cli"), "whisper-cli")
            self.python = tools.get("python") or _resolve_tool(None, "python3")
        else:  # tests and `--check` on a machine without the SDR stack
            self.rtl_fm = tools.get("rtl_fm", "rtl_fm")
            self.ffmpeg = tools.get("ffmpeg", "ffmpeg")
            self.whisper_bin = tools.get("whisper_cli", "whisper-cli")
            self.python = tools.get("python", "python3")

    def _validate_notify(self) -> None:
        if self.notify_mode == "command" and not self.notify_command:
            raise ConfigError("[notify].mode is 'command' but [notify].command is unset")
        if self.notify_mode == "webhook" and not self.notify_url:
            raise ConfigError("[notify].mode is 'webhook' but [notify].url is unset")
        if self.notify_mode == "webhook" and not str(self.notify_url).startswith("https://"):
            raise ConfigError("[notify].url must be https — alerts leave the machine over it")
        if self.notify_mode == "spool" and not self.notify_spool_dir:
            raise ConfigError("[notify].mode is 'spool' but [notify].spool_dir is unset")

    # --- derived paths -------------------------------------------------
    @property
    def capture_dir(self) -> Path:
        return self.home / "captures"

    @property
    def transcript_dir(self) -> Path:
        return self.home / "transcripts"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "watch_state.json"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "recorder.pid"

    @property
    def end_file(self) -> Path:
        return self.state_dir / "end_epoch"

    @property
    def hold_file(self) -> Path:
        """Touched by manual SDR sessions; while fresh the watcher defers."""
        return self.state_dir / "manual_hold"

    @property
    def hold_max_secs(self) -> int:
        return 3600  # a crashed manual session must not block storm recording forever

    @property
    def record_script(self) -> Path:
        """The recorder shell script.

        Ships inside the package and is resolved relative to this module, so
        installing the package is the only step — there is no copy for a user to
        forget. Forgetting it used to mean the watcher spawned a file that was
        never there, failing only once a warning fired.

        Override with [paths].record_script if you have modified the chain.
        """
        if self._record_script_override:
            path = Path(self._record_script_override).expanduser()
            if not path.is_file():
                raise ConfigError(
                    f"[paths].record_script points at {path}, which does not exist"
                )
            return path
        return Path(__file__).resolve().parent / "record.sh"


def load(path: Path | None = None, resolve_tools: bool = True) -> Config:
    path = path or find_config_path()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}")
    return Config(data, resolve_tools=resolve_tools)
