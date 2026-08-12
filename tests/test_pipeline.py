"""Chunker flush rules and transcript assembly. No audio hardware, no whisper."""

import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stormnet.chunker import Chunker  # noqa: E402
from stormnet import transcribe as tr  # noqa: E402

RATE = 24000


def fake_cfg(home: Path, **over):
    base = dict(
        sample_rate=RATE,
        chunk_bytes=RATE * 2 * 2,      # 2 s of audio per chunk, to keep tests small
        min_chunk_bytes=RATE * 2 // 2,  # 0.5 s floor
        max_wall_secs=180,
        home=home,
        capture_dir=home / "captures",
        transcript_dir=home / "transcripts",
        python="/usr/bin/python3",
        ffmpeg="/usr/bin/ffmpeg",
        whisper_bin="/usr/bin/whisper-cli",
        whisper_model="/tmp/model.bin",
        whisper_timeout=300,
        vocab_prompt="callsigns and towns",
    )
    base.update(over)
    return SimpleNamespace(**base)


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.cfg = fake_cfg(self.home)

    def tearDown(self):
        self._tmp.cleanup()


class TestChunkerFlush(PipelineCase):
    def _run_over(self, data: bytes, **cfg_over):
        """Feed `data` through the chunker from a file, collect chunk paths.

        Deliberately not a pipe: macOS pipe capacity is 65536 bytes, and these
        fixtures are larger, so writing the whole payload before starting the
        reader deadlocks. A regular file is always select()-ready and returns
        b"" at EOF — the same end-of-session condition rtl_fm's exit produces.
        """
        if cfg_over:
            self.cfg = fake_cfg(self.home, **cfg_over)
        got = []
        chunker = Chunker(self.cfg, on_chunk=got.append)
        src = self.home / "stream.raw"
        src.write_bytes(data)
        fd = os.open(src, os.O_RDONLY)
        try:
            count = chunker.run(fd)
        finally:
            os.close(fd)
        return count, got

    def test_full_buffer_flushes_a_chunk(self):
        count, chunks = self._run_over(b"\x01\x00" * (RATE * 2))  # exactly 2 s
        self.assertEqual(count, 1)
        self.assertEqual(len(chunks), 1)

    def test_multiple_chunks_from_a_long_stream(self):
        count, _ = self._run_over(b"\x01\x00" * (RATE * 4))  # 4 s -> two 2 s chunks
        self.assertEqual(count, 2)

    def test_eof_flushes_the_remainder(self):
        # 1 s: under chunk_bytes, over min_chunk_bytes -> flushed at EOF
        count, _ = self._run_over(b"\x01\x00" * RATE)
        self.assertEqual(count, 1)

    def test_a_scrap_below_the_floor_is_not_transcribed(self):
        # 0.1 s: below min_chunk_bytes -> dropped rather than sent to whisper
        count, _ = self._run_over(b"\x01\x00" * (RATE // 10))
        self.assertEqual(count, 0)

    def test_silence_produces_no_chunks(self):
        count, _ = self._run_over(b"")
        self.assertEqual(count, 0)

    def test_chunk_is_a_valid_mono_wav_at_the_configured_rate(self):
        _, chunks = self._run_over(b"\x01\x00" * (RATE * 2))
        with wave.open(str(chunks[0]), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getframerate(), RATE)
            self.assertEqual(w.getnframes(), RATE * 2)

    def test_transcriber_failure_does_not_lose_the_session(self):
        def boom(_wav):
            raise OSError("spawn failed")
        # Goes through _run_over deliberately: a hand-rolled os.pipe() here
        # deadlocks on the same 65536-byte capacity limit documented above.
        chunker = Chunker(self.cfg, on_chunk=boom)
        src = self.home / "boom.raw"
        src.write_bytes(b"\x01\x00" * (RATE * 2))
        fd = os.open(src, os.O_RDONLY)
        try:
            count = chunker.run(fd)   # the OSError must not propagate
        finally:
            os.close(fd)
        self.assertEqual(count, 1)


class TestTranscribe(PipelineCase):
    def test_ffmpeg_resamples_to_16k_and_whisper_gets_the_prompt(self):
        wav = self.home / "stormnet_20260706_143205_001.wav"
        wav.write_bytes(b"")
        done = subprocess.CompletedProcess([], 0, stdout="net control, go ahead", stderr="")
        with mock.patch.object(tr.subprocess, "run", return_value=done) as run:
            text = tr.transcribe(self.cfg, wav, run=run)
        self.assertEqual(text, "net control, go ahead")
        ffmpeg_cmd, whisper_cmd = run.call_args_list[0][0][0], run.call_args_list[1][0][0]
        self.assertIn("16000", ffmpeg_cmd)
        self.assertIn("--prompt", whisper_cmd)
        self.assertIn("callsigns and towns", whisper_cmd)

    def test_empty_vocab_prompt_omits_the_flag(self):
        cfg = fake_cfg(self.home, vocab_prompt="   ")
        wav = self.home / "stormnet_20260706_143205_001.wav"
        wav.write_bytes(b"")
        done = subprocess.CompletedProcess([], 0, stdout="text", stderr="")
        with mock.patch.object(tr.subprocess, "run", return_value=done) as run:
            tr.transcribe(cfg, wav, run=run)
        self.assertNotIn("--prompt", run.call_args_list[1][0][0])

    def test_chunk_stamp_parses_the_filename(self):
        self.assertEqual(
            tr.chunk_stamp(Path("stormnet_20260706_143205_001.wav")), "14:32:05")

    def test_chunk_stamp_falls_back_on_an_odd_name(self):
        self.assertEqual(tr.chunk_stamp(Path("weird.wav")), "weird")

    def test_append_writes_a_stamped_block(self):
        wav = Path("stormnet_20260706_143205_001.wav")
        out = tr.append_transcript(self.cfg, "hail one inch", wav, day="2026-07-06")
        self.assertIsNotNone(out)
        body = out.read_text(encoding="utf-8")
        self.assertIn("--- 14:32:05", body)
        self.assertIn("hail one inch", body)

    def test_appends_accumulate_in_one_file(self):
        wav = Path("stormnet_20260706_143205_001.wav")
        tr.append_transcript(self.cfg, "first", wav, day="2026-07-06")
        out = tr.append_transcript(self.cfg, "second", wav, day="2026-07-06")
        body = out.read_text(encoding="utf-8")
        self.assertIn("first", body)
        self.assertIn("second", body)

    def test_noise_markers_are_not_written(self):
        wav = Path("stormnet_20260706_143205_001.wav")
        for marker in ("", ".", "[BLANK_AUDIO]", "(static)", "[inaudible]"):
            self.assertIsNone(
                tr.append_transcript(self.cfg, marker, wav, day="2026-07-06"))
        self.assertFalse((self.cfg.transcript_dir / "stormnet_2026-07-06.txt").exists())


if __name__ == "__main__":
    unittest.main()
