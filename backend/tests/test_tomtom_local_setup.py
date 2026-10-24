"""Local credential loading and diagnostics must never execute or echo keys."""

import importlib.util
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_tomtom_access.py"
spec = importlib.util.spec_from_file_location("check_tomtom_local_access_test", SCRIPT)
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


class TomTomLocalAccessTests(unittest.TestCase):
    def test_read_named_keys_only_without_sourcing_shell_or_browser_prefixes(self):
        with TemporaryDirectory() as directory, patch.object(access, "ROOT", Path(directory)), patch.dict(access.os.environ, {}, clear=True):
            (Path(directory) / ".env.tomtom.local").write_text("TOMTOM_API_KEY=existing-key\nVITE_TOMTOM_API_KEY=must-not-load\nOTHER_SECRET=ignored\n$(touch /tmp/not-executed)\n")
            self.assertEqual(access.local_keys(), {"TOMTOM_API_KEY": "existing-key"})

    def test_explicit_environment_wins_and_stats_key_stays_separate(self):
        with TemporaryDirectory() as directory, patch.object(access, "ROOT", Path(directory)), patch.dict(access.os.environ, {"TOMTOM_API_KEY": "environment-key"}, clear=True):
            (Path(directory) / ".env.tomtom.local").write_text("TOMTOM_API_KEY=file-key\nTOMTOM_TRAFFIC_STATS_API_KEY='stats-key'\n")
            self.assertEqual(access.local_keys(), {"TOMTOM_API_KEY": "environment-key", "TOMTOM_TRAFFIC_STATS_API_KEY": "stats-key"})

    def test_upstream_error_does_not_echo_secret_bearing_urls_or_payloads(self):
        error = HTTPError("https://api.tomtom.com/?key=must-not-leak", 403, "must-not-leak", {}, io.BytesIO(b"must-not-leak"))
        with patch.object(access, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            result = access._check("/traffic/trafficstats/job/search/1", {}, "must-not-leak")
        self.assertEqual(result, {"status": "access_denied", "http_status": 403})
        self.assertNotIn("must-not-leak", str(result))

    def test_redirects_are_rejected_before_credentials_can_reach_another_host(self):
        handler = access.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://another-host.invalid"))


if __name__ == "__main__":
    unittest.main()
