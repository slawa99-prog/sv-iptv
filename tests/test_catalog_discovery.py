import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sv_iptv as sv


class CatalogDiscoveryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(sv, "ROOT", self.root)
        self.root_patch.start()
        self.now = "2026-11-08T12:00:00+00:00"
        self.base = [{"id": "A.ru", "name": "Первый тест", "group": "ТВ", "aliases": ["First Test"]}]
        self.cfg = {"catalog_discovery": {"enabled": True,
                    "metadata_url": "https://example.com/channels.json",
                    "playlist_url": "https://example.com/russian.m3u"},
                    "source_fallback_hours": 72}

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def discover(self, metadata, identities, now=None):
        text = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="{identity}",Source name\nhttps://example.com/{i}.m3u8\n'
            for i, identity in enumerate(identities))
        def fetch(url, *args, **kwargs):
            return json.dumps(metadata).encode() if url.endswith(".json") else text.encode()
        with patch.object(sv, "fetch_bytes", side_effect=fetch):
            return sv.discover_catalog(self.base, self.cfg, now or self.now)

    def test_discovers_one_channel_for_quality_variants(self):
        catalog, report = self.discover([
            {"id": "B.by", "name": "Second Test HD", "alt_names": ["Второй тест HD"],
             "categories": ["news"], "country": "BY"}], ["B.by@HD", "B.by@SD"])
        self.assertEqual(report["added"], 1)
        self.assertEqual(catalog[1]["name"], "Второй тест")
        self.assertEqual(catalog[1]["group"], "Новости")
        self.assertEqual(catalog[1]["country"], "BY")
        self.assertFalse((self.root / "tv.m3u").exists())

    def test_does_not_discover_closed_adult_unknown_or_time_shift_channels(self):
        metadata = [{"id": "Closed.ru", "name": "Closed", "closed": "2020-01-01"},
                    {"id": "Adult.ru", "name": "Adult", "is_nsfw": True},
                    {"id": "Replaced.ru", "name": "Replaced", "replaced_by": "A.ru"},
                    {"id": "Shift.ru", "name": "Shift"}]
        catalog, report = self.discover(metadata, ["Closed.ru@HD", "Adult.ru@HD", "Replaced.ru@SD",
                                                   "Shift.ru@Plus2", "Unknown.ru@SD"])
        self.assertEqual(catalog, self.base)
        self.assertEqual(report["added"], 0)

    def test_name_and_alias_collisions_never_create_duplicate_channels(self):
        catalog, report = self.discover([
            {"id": "B.ru", "name": "First Test"},
            {"id": "C.ru", "name": "Different", "alt_names": ["Первый тест"]},
            {"id": "D.ru", "name": "Первый тест HD"}], ["B.ru@HD", "C.ru@SD", "D.ru@HD"])
        self.assertEqual(catalog, self.base)
        self.assertEqual(report["excluded"]["name_collision"], 3)

    def test_explicitly_excluded_international_version_is_not_added(self):
        self.cfg["catalog_discovery"]["excluded_ids"] = ["BInternational.ru"]
        catalog, _ = self.discover([{"id": "BInternational.ru", "name": "International version"}],
                                   ["BInternational.ru@HD"])
        self.assertEqual(catalog, self.base)

    def test_recent_snapshot_survives_source_outage_but_expired_one_does_not(self):
        self.discover([{"id": "B.by", "name": "Second"}], ["B.by@HD"])
        with patch.object(sv, "fetch_bytes", side_effect=OSError("offline")):
            catalog, report = sv.discover_catalog(self.base, self.cfg, "2026-11-09T12:00:00+00:00")
            self.assertEqual(len(catalog), 2)
            self.assertEqual(report["state"], "snapshot")
            catalog, report = sv.discover_catalog(self.base, self.cfg, "2026-11-12T12:00:01+00:00")
            self.assertEqual(catalog, self.base)
            self.assertEqual(report["state"], "failed")

    def test_snapshot_cannot_be_reused_for_another_catalog(self):
        self.discover([{"id": "B.by", "name": "Second"}], ["B.by@HD"])
        self.cfg["catalog_discovery"]["playlist_url"] = "https://example.com/other.m3u"
        with patch.object(sv, "fetch_bytes", side_effect=OSError("offline")):
            catalog, report = sv.discover_catalog(self.base, self.cfg, self.now)
        self.assertEqual(catalog, self.base)
        self.assertEqual(report["state"], "failed")

    def test_local_check_does_not_inherit_or_modify_cloud_history(self):
        (self.root / "channels.json").write_text(json.dumps(self.base))
        (self.root / "custom.m3u").write_text("#EXTM3U\n")
        (self.root / "history.json").write_text('{"cloud": true}')
        (self.root / "tv.m3u").write_text("cloud playlist")
        local, cfg = sv.prepare_local_workspace({"workers": 6, "skip_tagged_geo_blocked": True})
        self.assertFalse((local / "history.json").exists())
        self.assertFalse((local / "tv.m3u").exists())
        self.assertFalse(cfg["skip_tagged_geo_blocked"])
        self.assertEqual(cfg["check_environment"], "local-network")
        self.assertEqual(cfg["workers"], 4)
        (local / "history.json").write_text('{"local": true}')
        sv.prepare_local_workspace({})
        self.assertEqual((local / "history.json").read_text(), '{"local": true}')
        self.assertEqual((self.root / "history.json").read_text(), '{"cloud": true}')
        self.assertEqual((self.root / "tv.m3u").read_text(), "cloud playlist")

    def test_api_streams_include_new_discovered_channel_and_require_live_pass(self):
        (self.root / "channels.json").write_text(json.dumps(self.base))
        self.cfg.update(sources=[{"url": "https://example.com/streams.json", "format": "iptv-org-streams"}],
                        minimum_channels=1, workers=2, require_fresh_remote_source=True)
        payloads = {
            "https://example.com/channels.json": json.dumps([{"id": "B.by", "name": "Second"}]).encode(),
            "https://example.com/russian.m3u": b'#EXTM3U\n#EXTINF:-1 tvg-id="B.by@HD",Second\nhttps://example.com/new.m3u8\n',
            "https://example.com/streams.json": json.dumps([
                {"channel": "A.ru", "feed": "HD", "title": "First", "url": "https://example.com/old.m3u8"},
                {"channel": "B.by", "feed": "HD", "title": "Second", "url": "https://example.com/new.m3u8"},
                {"channel": "C.xx", "feed": "HD", "title": "Unrelated", "url": "https://example.com/other.m3u8"}]).encode()}
        tested = []
        def checker(item, cfg):
            tested.append(item["url"])
            return {"ok": item["url"].endswith("old.m3u8"), "reason": "test", "media_url": item["url"]}
        with patch.object(sv, "fetch_bytes", side_effect=lambda url, *a, **kw: payloads[url]), \
             contextlib.redirect_stdout(io.StringIO()):
            report = sv.build(self.cfg, tester=checker, now=self.now)
        self.assertEqual(report["catalog_channels"], 2)
        self.assertEqual(report["catalog_discovery"]["added"], 1)
        self.assertEqual(report["unique_channels_passed"], 1)
        self.assertEqual(set(tested), {"https://example.com/old.m3u8", "https://example.com/new.m3u8"})
        self.assertNotIn("new.m3u8", (self.root / "tv.m3u").read_text())
        history = json.loads((self.root / "history.json").read_text())
        self.assertEqual(sum(not h["last_result"]["ok"] for h in history["streams"].values()), 1)


if __name__ == "__main__":
    unittest.main()
