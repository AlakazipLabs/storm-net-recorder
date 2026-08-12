"""Config loading and validation. No filesystem outside tmp, no network."""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stormnet import config as cfgmod  # noqa: E402

MINIMAL = """
[location]
latitude = 37.6872
longitude = -97.3301

[nws]
contact = "you@example.com"

[sdr]
frequency = "147.180M"

[whisper]
model = "/tmp/model.bin"
"""


def write_cfg(tmp: str, body: str) -> Path:
    path = Path(tmp) / "config.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestLoad(unittest.TestCase):
    def test_minimal_config_loads_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfgmod.load(write_cfg(tmp, MINIMAL), resolve_tools=False)
            self.assertEqual(cfg.freq, "147.180M")
            self.assertEqual(cfg.poll_secs, 120)
            self.assertEqual(cfg.notify_mode, "none")
            self.assertIn("Tornado Warning", cfg.trigger_events)

    def test_alerts_url_is_built_from_the_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfgmod.load(write_cfg(tmp, MINIMAL), resolve_tools=False)
            self.assertIn("point=37.6872,-97.3301", cfg.alerts_url)

    def test_chunk_bytes_derive_from_rate_and_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfgmod.load(write_cfg(tmp, MINIMAL + """
                [audio]
                chunk_secs = 10
                """), resolve_tools=False)
            self.assertEqual(cfg.chunk_bytes, 24000 * 2 * 10)

    def test_missing_required_key_names_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL.replace('frequency = "147.180M"', "")
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.load(write_cfg(tmp, body), resolve_tools=False)
            self.assertIn("[sdr].frequency", str(ctx.exception))

    def test_bad_toml_is_reported_as_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(write_cfg(tmp, "this is not toml ["), resolve_tools=False)


class TestValidation(unittest.TestCase):
    def test_contact_must_look_like_an_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL.replace('contact = "you@example.com"', 'contact = "nobody"')
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.load(write_cfg(tmp, body), resolve_tools=False)
            self.assertIn("contact", str(ctx.exception))

    def test_empty_trigger_events_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL + "\n[nws.trigger_events]\n"
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.load(write_cfg(tmp, body), resolve_tools=False)
            self.assertIn("trigger_events", str(ctx.exception))

    def test_unknown_notify_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL + '\n[notify]\nmode = "carrier-pigeon"\n'
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(write_cfg(tmp, body), resolve_tools=False)

    def test_command_mode_requires_a_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL + '\n[notify]\nmode = "command"\n'
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(write_cfg(tmp, body), resolve_tools=False)

    def test_webhook_must_be_https(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL + '\n[notify]\nmode = "webhook"\nurl = "http://example.com/h"\n'
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.load(write_cfg(tmp, body), resolve_tools=False)
            self.assertIn("https", str(ctx.exception))

    def test_spool_mode_requires_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL + '\n[notify]\nmode = "spool"\n'
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load(write_cfg(tmp, body), resolve_tools=False)


class TestDiscovery(unittest.TestCase):
    def test_env_pointing_at_a_missing_file_is_an_error(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.find_config_path({"STORMNET_CONFIG": "/nonexistent/config.toml"})

    def test_env_wins_when_the_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cfg(tmp, MINIMAL)
            found = cfgmod.find_config_path({"STORMNET_CONFIG": str(path)})
            self.assertEqual(found, path)


class TestToolResolution(unittest.TestCase):
    def test_missing_tool_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = MINIMAL + '\n[tools]\nrtl_fm = "/nonexistent/rtl_fm"\n'
            with self.assertRaises(cfgmod.ConfigError) as ctx:
                cfgmod.load(write_cfg(tmp, body), resolve_tools=True)
            self.assertIn("rtl_fm", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
