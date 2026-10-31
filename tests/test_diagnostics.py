import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from free_my_mac import measure, scan, safe_text


class DiagnosticsTests(unittest.TestCase):
    def test_preserves_artifacts_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "sketchpad.bin"
            artifact.write_bytes(b"artifact" * 1024)
            (root / "link").symlink_to(artifact)
            before = artifact.read_bytes()
            rows = {p.name: (size, status) for p, size, status in scan(root)}
            self.assertEqual(artifact.read_bytes(), before)
            self.assertEqual(set(rows), {"sketchpad.bin", "link"})
            self.assertIsNone(rows["link"][0])
            self.assertIn("preservar", rows["sketchpad.bin"][1])

    @patch("free_my_mac.subprocess.run")
    def test_partial_measurement_is_not_reported_as_complete(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "42\t/test", "denied")
        self.assertIsNone(measure(Path("/test"))[0])

    @patch("free_my_mac.subprocess.run", side_effect=subprocess.TimeoutExpired("du", 30))
    def test_timeout_is_unknown_not_zero(self, run):
        self.assertIsNone(measure(Path("/test"))[0])

    def test_cancel_skips_remaining_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "file").touch()
            stop = threading.Event()
            stop.set()
            self.assertEqual(scan(Path(directory), stop), [])

    def test_terminal_control_characters_are_sanitized(self):
        self.assertEqual(safe_text("a\x1b\nb"), "a??b")


if __name__ == "__main__":
    unittest.main()
