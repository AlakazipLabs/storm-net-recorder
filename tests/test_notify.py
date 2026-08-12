"""Notifier dispatch across all four modes. No network, no subprocesses."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stormnet.notify import Notifier  # noqa: E402


def fake_cfg(**over):
    base = dict(
        notify_mode="none", notify_command=None, notify_url=None,
        notify_spool_dir=None, notify_source="storm-net-recorder",
        notify_text_max=3500, user_agent="(storm-net-recorder, you@example.com)",
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestBuild(unittest.TestCase):
    def test_alert_shape(self):
        n = Notifier(fake_cfg(), now_iso=lambda: "2026-08-12T12:00:00+00:00")
        alert = n.build("warn", "T", "body", "k")
        self.assertEqual(alert, {
            "source": "storm-net-recorder", "level": "warn", "title": "T",
            "text": "body", "ts": "2026-08-12T12:00:00+00:00", "dedup_key": "k",
        })

    def test_text_is_truncated(self):
        n = Notifier(fake_cfg(notify_text_max=10))
        self.assertEqual(len(n.build("info", "T", "x" * 500, "k")["text"]), 10)

    def test_bad_level_rejected(self):
        with self.assertRaises(ValueError):
            Notifier(fake_cfg()).build("catastrophic", "T", "b", "k")


class TestNoneMode(unittest.TestCase):
    def test_logs_and_succeeds(self):
        self.assertTrue(Notifier(fake_cfg()).send("info", "T", "b", "k"))


class TestCommandMode(unittest.TestCase):
    def test_alert_json_goes_to_stdin(self):
        cfg = fake_cfg(notify_mode="command", notify_command=["/usr/bin/true", "--x"])
        n = Notifier(cfg)
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=done) as run:
            self.assertTrue(n.send("warn", "T", "b", "k"))
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(argv, ["/usr/bin/true", "--x"])
        self.assertEqual(json.loads(kwargs["input"])["title"], "T")
        self.assertIn("timeout", kwargs)

    def test_nonzero_exit_is_a_failure_not_a_crash(self):
        cfg = fake_cfg(notify_mode="command", notify_command=["/bin/false"])
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="nope")
        with mock.patch("subprocess.run", return_value=done):
            self.assertFalse(Notifier(cfg).send("warn", "T", "b", "k"))

    def test_string_command_is_refused_rather_than_shell_split(self):
        cfg = fake_cfg(notify_mode="command", notify_command="/bin/echo hi")
        # Swallowed into False rather than raised — a bad notifier config must
        # not take down the watcher mid-storm.
        self.assertFalse(Notifier(cfg).send("warn", "T", "b", "k"))

    def test_timeout_is_swallowed(self):
        cfg = fake_cfg(notify_mode="command", notify_command=["/bin/sleep"])
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("x", 30)):
            self.assertFalse(Notifier(cfg).send("warn", "T", "b", "k"))


class TestWebhookMode(unittest.TestCase):
    def test_posts_json(self):
        cfg = fake_cfg(notify_mode="webhook", notify_url="https://example.com/h")
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        with mock.patch("urllib.request.urlopen", return_value=resp) as opener:
            self.assertTrue(Notifier(cfg).send("crit", "T", "b", "k"))
        req = opener.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data)["level"], "crit")

    def test_server_error_is_a_failure(self):
        cfg = fake_cfg(notify_mode="webhook", notify_url="https://example.com/h")
        resp = mock.MagicMock()
        resp.status = 500
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertFalse(Notifier(cfg).send("crit", "T", "b", "k"))

    def test_network_failure_does_not_propagate(self):
        cfg = fake_cfg(notify_mode="webhook", notify_url="https://example.com/h")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(Notifier(cfg).send("crit", "T", "b", "k"))


class TestSpoolMode(unittest.TestCase):
    def test_writes_one_json_file_per_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            cfg = fake_cfg(notify_mode="spool", notify_spool_dir=str(spool))
            n = Notifier(cfg, clock=lambda: 1_000_000.0)
            self.assertTrue(n.send("warn", "T", "body", "k1"))
            files = list(spool.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["dedup_key"], "k1")

    def test_no_temp_files_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            cfg = fake_cfg(notify_mode="spool", notify_spool_dir=str(spool))
            n = Notifier(cfg, clock=lambda: 1_000_000.0)
            n.send("warn", "T", "b", "k1")
            self.assertEqual(list(spool.glob("*.tmp")), [])

    def test_filenames_sort_in_arrival_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            cfg = fake_cfg(notify_mode="spool", notify_spool_dir=str(spool))
            n = Notifier(cfg, clock=lambda: 1_000_000.0)  # same ms for all three
            for i in range(3):
                n.send("info", "T", f"body{i}", f"k{i}")
            names = sorted(p.name for p in spool.glob("*.json"))
            bodies = [json.loads((spool / name).read_text())["text"] for name in names]
            self.assertEqual(bodies, ["body0", "body1", "body2"])

    def test_directory_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "spool"
            cfg = fake_cfg(notify_mode="spool", notify_spool_dir=str(spool))
            Notifier(cfg)
            self.assertEqual(spool.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
