"""Trace integrity checks; run with unittest discover -s tools/replay."""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay


class TraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "trace.bin"

    def trace_bytes(self, fields: list[str] | None = None) -> bytes:
        fields = replay.REQUIRED_FIELDS if fields is None else fields
        header = struct.pack("<4sIII", b"TRCE", 1, len(fields), 4 * len(fields))
        names = ("\n".join(fields) + "\ninstalled\n").encode("ascii")
        record = tuple(replay.TITLE if field == "kind" else 0 for field in fields)
        return header + names + struct.pack(f"<{len(fields)}I", *record)

    def read(self, data: bytes) -> replay.Trace:
        self.path.write_bytes(data)
        return replay.read_trace(self.path)

    def test_complete_trace(self) -> None:
        trace = self.read(self.trace_bytes())
        self.assertEqual(trace.info, "installed")
        self.assertEqual(len(trace.of(replay.TITLE)), 1)

    def test_partial_record_is_rejected_even_after_title(self) -> None:
        # Previously discarded silently, allowing a corrupt trace to match.
        with self.assertRaisesRegex(ValueError, "truncated trace record"):
            self.read(self.trace_bytes() + b"\x01")

    def test_truncated_headers(self) -> None:
        for data in (b"", b"TRCE", self.trace_bytes()[:20]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.read(data)

    def test_zero_width_record(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a version 1 trace"):
            self.read(struct.pack("<4sIII", b"TRCE", 1, 0, 0))

    def test_duplicate_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.read(self.trace_bytes(replay.REQUIRED_FIELDS + ["score"]))

    def test_missing_required_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "no score"):
            self.read(self.trace_bytes(replay.REQUIRED_FIELDS[:-1]))

    def test_kind_is_resolved_by_name(self) -> None:
        fields = ["score", *replay.REQUIRED_FIELDS[:-1]]
        left = self.read(self.trace_bytes(fields))
        right = replay.Trace(fields, left.info, [tuple(1 if field == "score" else value
                            for field, value in zip(fields, left.records[0]))])
        result = replay.compare(left, right)
        self.assertEqual(result["first_difference"]["kind"], "title")


class FetchTest(unittest.TestCase):
    PAGE = ('<table id="X_table"><tr><th>Shottype</th><th>Easy</th><th>Normal</th></tr>'
            '<tr><td>Border Team</td><td><a href="https://example.org/a.rpy">1</a><br>by <em>P&amp;Q</em>'
            '<span class="dimgrey"><br>01/02/2003</span></td>'
            '<td><a href="https://example.org/b.rpy">2</a><br>by <em>R</em></td></tr></table>')

    def test_spaced_shot_names_and_rejected_downloads(self) -> None:
        class Replay:
            def __init__(self, path: str) -> None:
                data = Path(path).read_bytes()
                if not data:
                    raise ValueError(f"{path}: empty")
                self.label, self.score = data.decode(), 10

        files = {"https://maribelhearn.com/wr": self.PAGE.encode(), "https://example.org/a.rpy": b"Border Team Easy",
                 "https://example.org/b.rpy": b""}
        hook = SimpleNamespace(WR_TABLE="X_table", Replay=Replay)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(replay, "REPLAYS", directory), \
                patch.object(replay, "MANIFEST", os.path.join(directory, "manifest.json")), \
                patch.object(replay, "hook", return_value=hook), \
                patch.object(replay.urllib.request, "urlopen", side_effect=lambda r: io.BytesIO(files[r.full_url])), \
                patch("sys.stdout", io.StringIO()):
            replay.fetch()
            manifest = json.loads(Path(directory, "manifest.json").read_text())
            self.assertEqual(sorted(os.listdir(directory)), ["a.rpy", "manifest.json"])
        [entry] = manifest["replays"]
        self.assertEqual((entry["shot"], entry["difficulty"], entry["player"], entry["date"]),
                         ("Border Team", "Easy", "P&Q", "01/02/2003"))
        self.assertEqual(manifest["skipped"], [dict(file="b.rpy", shot="Border Team", difficulty="Normal",
                                                    url="https://example.org/b.rpy", reason="empty")])


if __name__ == "__main__":
    unittest.main()
