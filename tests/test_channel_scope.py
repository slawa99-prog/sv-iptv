import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sv_iptv as sv


class ChannelScopeTests(unittest.TestCase):
    def setUp(self):
        self.channels = [{"id": "Russian.ru", "name": "Русский", "group": "ТВ"},
                         {"id": "Foreign.ru", "name": "Foreign", "group": "ТВ"}]
        self.cfg = {"channel_scope": {"mode": "reviewed-allowlist",
                    "allowed_ids": ["RUSSIAN.RU"], "excluded_ids": ["Foreign.ru"]}}
        self.records = [{"url": f"https://example.com/{i}.m3u8", "name": c["name"],
                         "attrs": {"tvg-id": c["id"]}, "headers": {"User-Agent": "TV player"}}
                        for i, c in enumerate(self.channels)]

    def test_source_refresh_cannot_reintroduce_foreign_or_unreviewed_channel(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sv, "ROOT", Path(tmp)):
            sv.write_json(Path(tmp) / "channels.json", self.channels)
            discovered = self.channels + [{"id": "New.ru", "name": "New", "group": "ТВ"}]
            with patch.object(sv, "discover_catalog", return_value=(discovered, {})), \
                 patch.object(sv, "load_sources", return_value=(self.records, [])), \
                 contextlib.redirect_stdout(io.StringIO()):
                report = sv.build(self.cfg, inventory=True)
            self.assertEqual(report["catalog_channels"], 1)
            self.assertEqual(report["catalog_discovery"]["excluded_by_playlist_scope"], 2)
            entries = sv.parse_m3u((Path(tmp) / "candidates-unverified.m3u").read_text())
            self.assertEqual([e["attrs"]["tvg-id"] for e in entries], ["Russian.ru"])
            self.assertEqual(entries[0]["headers"], self.records[0]["headers"])

    def test_explicit_foreign_id_cannot_alias_to_russian_channel(self):
        item = {**self.records[1], "name": "Русский", "match_by_name": True}
        found, excluded = sv.group_candidates([item], self.channels[:1], self.cfg)
        self.assertEqual(found["russian.ru"], [])
        self.assertEqual(excluded["outside_playlist_scope"], 1)

    def test_direct_export_enforces_scope_and_invalid_scope_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sv, "ROOT", Path(tmp)):
            text = sv.render_m3u(list(zip(self.channels, self.records)), self.cfg)
            self.assertNotIn("Foreign.ru", text)
            self.assertIn("Russian.ru", text)
            with self.assertRaises(ValueError):
                sv.render_m3u([], {"channel_scope": {"mode": "reviewed-allowlist", "allowed_ids": []}})


if __name__ == "__main__":
    unittest.main()
