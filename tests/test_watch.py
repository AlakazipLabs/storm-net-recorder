"""Watcher behavior: alert parsing, deferral, recorder lifecycle, state pruning.

No network and no processes: fetch is injected, spawn is mocked.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stormnet.watch import Watcher  # noqa: E402

NOW = 1_800_000_000.0


def fake_cfg(home: Path):
    return SimpleNamespace(
        lat=37.6872, lon=-97.3301,
        alerts_url="https://api.weather.gov/alerts/active?point=37.6872,-97.3301",
        user_agent="(storm-net-recorder, you@example.com)",
        http_timeout=15, poll_secs=120, grace_secs=120,
        trigger_events={"Severe Thunderstorm Warning": "warn",
                        "Tornado Warning": "crit"},
        freq="147.180M", gain="42", squelch="50", sample_rate=24000,
        rtl_fm="/usr/bin/rtl_fm", python="/usr/bin/python3",
        home=home,
        capture_dir=home / "captures",
        transcript_dir=home / "transcripts",
        state_dir=home / "state",
        state_file=home / "state/watch_state.json",
        pid_file=home / "state/recorder.pid",
        end_file=home / "state/end_epoch",
        hold_file=home / "state/manual_hold",
        hold_max_secs=3600,
        record_script=home / "record.sh",
    )


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, level, title, text, dedup_key):
        self.sent.append({"level": level, "title": title,
                          "text": text, "dedup_key": dedup_key})
        return True


def payload(*features):
    return {"features": list(features)}


def warning(event="Severe Thunderstorm Warning", ends="2027-01-15T18:00:00+00:00",
            alert_id="urn:oid:1.2.3"):
    return {"properties": {"event": event, "ends": ends,
                           "headline": f"{event} for Somewhere", "id": alert_id}}


class WatcherCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "state").mkdir(parents=True)
        self.cfg = fake_cfg(self.home)
        self.notifier = FakeNotifier()
        self.w = Watcher(self.cfg, notifier=self.notifier)

    def tearDown(self):
        self._tmp.cleanup()


class TestParseWarnings(WatcherCase):
    def test_selects_only_configured_events(self):
        got = self.w.parse_warnings(
            payload(warning(event="Special Weather Statement"),
                    warning(event="Tornado Warning", alert_id="t1")), NOW)
        self.assertEqual([w["event"] for w in got], ["Tornado Warning"])

    def test_watches_do_not_trigger(self):
        got = self.w.parse_warnings(payload(warning(event="Tornado Watch")), NOW)
        self.assertEqual(got, [])

    def test_expired_warnings_are_dropped(self):
        got = self.w.parse_warnings(
            payload(warning(ends="2020-01-01T00:00:00+00:00")), NOW)
        self.assertEqual(got, [])

    def test_unparseable_end_time_is_skipped_not_fatal(self):
        got = self.w.parse_warnings(payload(warning(ends="soon-ish")), NOW)
        self.assertEqual(got, [])

    def test_falls_back_to_expires_when_ends_absent(self):
        feature = {"properties": {"event": "Tornado Warning",
                                  "expires": "2027-01-15T18:00:00+00:00",
                                  "id": "x1"}}
        self.assertEqual(len(self.w.parse_warnings(payload(feature), NOW)), 1)

    def test_warning_without_an_id_is_skipped(self):
        feature = {"properties": {"event": "Tornado Warning",
                                  "ends": "2027-01-15T18:00:00+00:00"}}
        self.assertEqual(self.w.parse_warnings(payload(feature), NOW), [])

    def test_empty_payload(self):
        self.assertEqual(self.w.parse_warnings({}, NOW), [])


class TestPollBehavior(WatcherCase):
    def test_new_warning_notifies_and_starts_recorder(self):
        with mock.patch.object(self.w, "ensure_recorder", return_value=True) as start:
            self.w.poll_once(now=NOW, fetch=lambda: payload(warning()))
        start.assert_called_once()
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0]["level"], "warn")

    def test_same_warning_does_not_notify_twice(self):
        fetch = lambda: payload(warning())  # noqa: E731
        with mock.patch.object(self.w, "ensure_recorder", return_value=True):
            self.w.poll_once(now=NOW, fetch=fetch)
            self.w.poll_once(now=NOW + 120, fetch=fetch)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_tornado_warning_raises_the_level(self):
        with mock.patch.object(self.w, "ensure_recorder", return_value=True):
            self.w.poll_once(now=NOW, fetch=lambda: payload(
                warning(event="Tornado Warning", alert_id="t9")))
        self.assertEqual(self.notifier.sent[0]["level"], "crit")

    def test_end_epoch_written_with_grace(self):
        with mock.patch.object(self.w, "ensure_recorder", return_value=True):
            self.w.poll_once(now=NOW, fetch=lambda: payload(warning()))
        written = int(self.cfg.end_file.read_text())
        from datetime import datetime
        ends = datetime.fromisoformat("2027-01-15T18:00:00+00:00").timestamp()
        self.assertEqual(written, int(ends + self.cfg.grace_secs))

    def test_latest_expiry_wins_when_warnings_overlap(self):
        two = payload(warning(alert_id="a", ends="2027-01-15T18:00:00+00:00"),
                      warning(alert_id="b", ends="2027-01-15T19:00:00+00:00"))
        with mock.patch.object(self.w, "ensure_recorder", return_value=True):
            self.w.poll_once(now=NOW, fetch=lambda: two)
        from datetime import datetime
        later = datetime.fromisoformat("2027-01-15T19:00:00+00:00").timestamp()
        self.assertEqual(int(self.cfg.end_file.read_text()),
                         int(later + self.cfg.grace_secs))

    def test_old_notified_ids_are_pruned(self):
        self.cfg.state_file.write_text(
            '{"notified": {"ancient": %d}, "session_active": false}'
            % int(NOW - 200_000))
        self.w.poll_once(now=NOW, fetch=lambda: payload())
        import json
        self.assertNotIn("ancient",
                         json.loads(self.cfg.state_file.read_text())["notified"])


class TestManualHold(WatcherCase):
    """The deferral rule: a manual session keeps the receiver."""

    def test_fresh_hold_defers_recording(self):
        self.cfg.hold_file.touch()
        with mock.patch.object(self.w, "ensure_recorder") as start, \
             mock.patch.object(self.w, "manual_hold_active", return_value=True):
            self.w.poll_once(now=NOW, fetch=lambda: payload(warning()))
        start.assert_not_called()

    def test_deferral_still_notifies_and_still_sets_the_end_time(self):
        self.cfg.hold_file.touch()
        with mock.patch.object(self.w, "ensure_recorder"), \
             mock.patch.object(self.w, "manual_hold_active", return_value=True):
            self.w.poll_once(now=NOW, fetch=lambda: payload(warning()))
        titles = [s["title"] for s in self.notifier.sent]
        self.assertIn("Recording deferred", titles)
        self.assertTrue(self.cfg.end_file.exists())

    def test_deferral_notice_is_sent_once_per_hold(self):
        self.cfg.hold_file.touch()
        fetch = lambda: payload(warning())  # noqa: E731
        with mock.patch.object(self.w, "ensure_recorder"), \
             mock.patch.object(self.w, "manual_hold_active", return_value=True):
            self.w.poll_once(now=NOW, fetch=fetch)
            self.w.poll_once(now=NOW + 120, fetch=fetch)
        deferrals = [s for s in self.notifier.sent if s["title"] == "Recording deferred"]
        self.assertEqual(len(deferrals), 1)

    def test_stale_hold_does_not_block_recording(self):
        import os
        self.cfg.hold_file.touch()
        old = NOW - (self.cfg.hold_max_secs + 60)
        os.utime(self.cfg.hold_file, (old, old))
        self.assertFalse(self.w.manual_hold_active(NOW))

    def test_absent_hold_file_is_not_a_hold(self):
        self.assertFalse(self.w.manual_hold_active(NOW))


class TestAllClear(WatcherCase):
    def test_all_clear_once_warnings_end_and_recorder_is_gone(self):
        self.cfg.state_file.write_text('{"notified": {}, "session_active": true}')
        with mock.patch.object(self.w, "recorder_alive", return_value=False):
            self.w.poll_once(now=NOW, fetch=lambda: payload())
        self.assertEqual([s["title"] for s in self.notifier.sent], ["All clear"])

    def test_no_all_clear_while_the_recorder_is_still_draining(self):
        self.cfg.state_file.write_text('{"notified": {}, "session_active": true}')
        with mock.patch.object(self.w, "recorder_alive", return_value=True):
            self.w.poll_once(now=NOW, fetch=lambda: payload())
        self.assertEqual(self.notifier.sent, [])


class TestRecorderLifecycle(WatcherCase):
    def test_spawn_passes_config_through_the_environment(self):
        proc = mock.MagicMock()
        proc.pid = 4242
        proc.poll.return_value = None
        with mock.patch.object(self.w, "recorder_alive", return_value=False):
            self.assertTrue(self.w.ensure_recorder(spawn=mock.MagicMock(return_value=proc)))
        self.assertEqual(self.cfg.pid_file.read_text(), "4242")

    def test_does_not_double_spawn(self):
        spawn = mock.MagicMock()
        with mock.patch.object(self.w, "recorder_alive", return_value=True):
            self.assertFalse(self.w.ensure_recorder(spawn=spawn))
        spawn.assert_not_called()

    def test_stale_pid_file_does_not_count_as_alive(self):
        self.cfg.pid_file.write_text("999999")
        self.assertFalse(self.w.recorder_alive())

    def test_garbage_pid_file_is_survivable(self):
        self.cfg.pid_file.write_text("not-a-pid")
        self.assertFalse(self.w.recorder_alive())


if __name__ == "__main__":
    unittest.main()
