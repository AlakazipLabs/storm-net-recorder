"""Transcribe one chunk wav and append it to today's transcript.

ffmpeg resamples to 16 kHz mono (whisper.cpp wants 16k, and ffmpeg's resampler
is better than asking rtl_fm for it), then whisper-cli runs with the local
vocabulary prompt from the config.

Appends under an exclusive flock, so overlapping chunk transcribers cannot
interleave their writes. Each block is headed with the chunk's start time, so
the transcript reads in order even when a slow chunk lands after a fast one.
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("stormnet.transcribe")

# whisper output that means "nothing was said" — keep it out of the transcript
NOISE_MARKERS = {"", ".", "[BLANK_AUDIO]", "(static)", "[inaudible]"}


def transcribe(cfg, wav: Path, run=subprocess.run) -> str:
    wav16 = wav.with_suffix(".16k.wav")
    run([cfg.ffmpeg, "-y", "-loglevel", "error", "-i", str(wav),
         "-ar", "16000", "-ac", "1", str(wav16)],
        check=True, capture_output=True, timeout=60)
    try:
        cmd = [cfg.whisper_bin, "-m", os.path.expanduser(cfg.whisper_model),
               "-f", str(wav16), "-nt", "-np"]
        if cfg.vocab_prompt.strip():
            cmd += ["--prompt", cfg.vocab_prompt.strip()]
        result = run(cmd, check=True, capture_output=True, text=True,
                     timeout=cfg.whisper_timeout)
    finally:
        try:
            os.unlink(wav16)
        except OSError:
            pass
    return result.stdout.strip()


def chunk_stamp(wav: Path) -> str:
    """stormnet_20260706_143205_001.wav -> '14:32:05' (falls back to the stem)."""
    try:
        parts = wav.stem.split("_")
        return datetime.strptime("_".join(parts[1:3]),
                                 "%Y%m%d_%H%M%S").strftime("%H:%M:%S")
    except (IndexError, ValueError):
        return wav.stem


def append_transcript(cfg, text: str, wav: Path, day: str | None = None) -> Path | None:
    if text.strip() in NOISE_MARKERS:
        log.info("chunk %s: no speech, skipped", wav.name)
        return None
    day = day or datetime.now().strftime("%Y-%m-%d")
    cfg.transcript_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.transcript_dir / f"stormnet_{day}.txt"
    block = f"--- {chunk_stamp(wav)} ({wav.name}) ---\n{text.strip()}\n\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(block)
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if len(argv) != 2:
        log.error("usage: python3 -m stormnet.transcribe <chunk.wav>")
        return 2
    wav = Path(argv[1])
    if not wav.is_file():
        log.error("no such wav: %s", wav)
        return 2
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    try:
        text = transcribe(cfg, wav)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("transcription failed for %s: %s", wav.name, e)
        return 1
    dest = append_transcript(cfg, text, wav)
    if dest:
        log.info("chunk %s -> %s (%d chars)", wav.name, dest.name, len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
