"""Chunker — raw s16le mono on stdin -> timestamped wav chunks -> transcriber.

Sits on the right of `rtl_fm ... | stormnet-chunk`. Because the recorder runs
rtl_fm with squelch, bytes arrive only while the channel is open: what reaches
this process is speech, not hiss. One chunk of `chunk_secs` of audio can
therefore span far more than `chunk_secs` of wall time.

Flush rules — audio is never discarded:
  buffer reaches chunk_bytes                              -> flush
  max_wall_secs elapsed AND buffer >= min_chunk_bytes      -> flush
  EOF (recorder killed rtl_fm) AND buffer >= min_chunk_bytes -> flush, then exit

select() with a timeout keeps the wall-clock rule alive even while the squelch
stays shut, which is most of any storm.
"""

from __future__ import annotations

import logging
import os
import select
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("stormnet.chunker")

READ_BYTES = 65536
SELECT_SECS = 5.0


class Chunker:
    def __init__(self, cfg, on_chunk=None):
        self.cfg = cfg
        self.on_chunk = on_chunk or self.spawn_transcriber

    def write_wav(self, buf: bytes, when: float, seq: int) -> Path:
        self.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(when).strftime("%Y%m%d_%H%M%S")
        path = self.cfg.capture_dir / f"stormnet_{stamp}_{seq:03d}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.cfg.sample_rate)
            w.writeframes(buf)
        return path

    def spawn_transcriber(self, wav: Path, spawn=subprocess.Popen) -> None:
        """Fire and forget: a slow chunk must not stall the next one."""
        logf = open(self.cfg.home / "transcribe.log", "ab")
        spawn(
            [self.cfg.python, "-m", "stormnet.transcribe", str(wav)],
            stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "STORMNET_HOME": str(self.cfg.home)},
        )
        log.info("chunk %s (%d bytes) -> transcriber", wav.name, wav.stat().st_size)

    def run(self, fd: int, clock=None, now=None) -> int:
        """Main loop; returns the number of chunks flushed."""
        import time
        clock = clock or time.monotonic
        now = now or time.time

        buf = bytearray()
        chunk_start_wall = clock()
        chunk_start_ts = now()
        chunks = 0

        def flush() -> None:
            nonlocal buf, chunk_start_wall, chunk_start_ts, chunks
            wav = self.write_wav(bytes(buf), chunk_start_ts, chunks)
            try:
                self.on_chunk(wav)
            except OSError:
                log.exception("transcriber spawn failed for %s", wav.name)
            chunks += 1
            buf = bytearray()
            chunk_start_wall = clock()
            chunk_start_ts = now()

        while True:
            ready, _, _ = select.select([fd], [], [], SELECT_SECS)
            if ready:
                data = os.read(fd, READ_BYTES)
                if not data:  # EOF: the recorder ended the session
                    if len(buf) >= self.cfg.min_chunk_bytes:
                        flush()
                    break
                if not buf:
                    chunk_start_ts = now()  # stamp = first audio in this chunk
                    chunk_start_wall = clock()
                buf += data
            if len(buf) >= self.cfg.chunk_bytes:
                flush()
            elif (clock() - chunk_start_wall >= self.cfg.max_wall_secs
                  and len(buf) >= self.cfg.min_chunk_bytes):
                flush()
        return chunks


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    n = Chunker(cfg).run(sys.stdin.fileno())
    log.info("session over, %d chunks", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
