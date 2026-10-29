import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import sv_iptv as sv


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(sv, "ROOT", self.root)
        self.root_patch.start()
        self.cfg = {"sources": ["input.m3u"], "minimum_channels": 1, "workers": 2,
                    "max_variants_per_channel": 4, "require_fresh_remote_source": False}

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def prepare(self, channels, m3u):
        (self.root / "channels.json").write_text(json.dumps(channels), encoding="utf-8")
        (self.root / "input.m3u").write_text(m3u, encoding="utf-8")

    def build(self, good=(), now="2026-10-27T00:00:00+00:00"):
        def checker(item, cfg):
            return {"ok": item["url"] in good, "reason": "ok" if item["url"] in good else "HTTP 503",
                    "pixels": 1280 * 720, "download_seconds": 2, "media_url": item["url"]}
        with contextlib.redirect_stdout(io.StringIO()):
            return sv.build(self.cfg, tester=checker, now=now)

    def test_quality_variants_one_channel_and_time_shift_excluded(self):
        self.prepare([{"id": "NTV.ru", "name": "НТВ", "group": "ТВ"}], '''#EXTM3U
#EXTINF:-1 tvg-id="NTV.ru@HD",НТВ HD
https://example.com/hd.m3u8
#EXTINF:-1 tvg-id="NTV.ru@SD",НТВ SD
https://example.com/sd.m3u8
#EXTINF:-1 tvg-id="NTV.ru@Plus2",НТВ +2
https://example.com/plus2.m3u8
''')
        r = self.build(["https://example.com/hd.m3u8", "https://example.com/sd.m3u8", "https://example.com/plus2.m3u8"])
        self.assertEqual(r["unique_channels_passed"], 1)
        text = (self.root / "tv.m3u").read_text()
        self.assertEqual(text.count("#EXTINF"), 1)
        self.assertNotIn("plus2", text)
        backup = json.loads((self.root / "backups.json").read_text())
        self.assertEqual(len(backup["channels"]["NTV.ru"]), 1)

    def test_same_stream_under_different_ids_is_not_two_channels(self):
        self.prepare([{"id": "A.ru", "name": "A", "group": "ТВ"}, {"id": "B.ru", "name": "B", "group": "ТВ"}], '''#EXTM3U
#EXTINF:-1 tvg-id="A.ru",A
https://example.com/same.m3u8
#EXTINF:-1 tvg-id="B.ru",B
https://example.com/same.m3u8
''')
        r = self.build(["https://example.com/same.m3u8"])
        self.assertEqual(r["unique_channels_passed"], 1)

    def test_failed_primary_switches_to_successful_backup(self):
        self.prepare([{"id": "A.ru", "name": "A", "group": "ТВ"}], '''#EXTM3U
#EXTINF:-1 tvg-id="A.ru",A
https://example.com/first.m3u8
#EXTINF:-1 tvg-id="A.ru",A
https://example.com/backup.m3u8
''')
        self.build(["https://example.com/first.m3u8"])
        r = self.build(["https://example.com/backup.m3u8"], now="2026-10-27T06:00:00+00:00")
        self.assertEqual(r["publication"], "published")
        self.assertIn("backup.m3u8", (self.root / "tv.m3u").read_text())
        self.assertNotIn("first.m3u8", (self.root / "tv.m3u").read_text())

    def test_count_guard_preserves_playlist_and_records_failure(self):
        self.cfg["minimum_channels"] = 2
        self.prepare([{"id": "A.ru", "name": "A", "group": "ТВ"}, {"id": "B.ru", "name": "B", "group": "ТВ"}], '''#EXTM3U
#EXTINF:-1 tvg-id="A.ru",A
https://example.com/a.m3u8
#EXTINF:-1 tvg-id="B.ru",B
https://example.com/b.m3u8
''')
        self.build(["https://example.com/a.m3u8", "https://example.com/b.m3u8"])
        original = (self.root / "tv.m3u").read_bytes()
        r = self.build(["https://example.com/a.m3u8"], now="2026-10-27T06:00:00+00:00")
        self.assertEqual(r["publication"], "blocked")
        self.assertEqual(r["last_published_count"], 2)
        self.assertEqual((self.root / "tv.m3u").read_bytes(), original)
        history = json.loads((self.root / "history.json").read_text())
        self.assertTrue(any(not v["checks"][-1]["ok"] for v in history["streams"].values()))

    def test_first_run_below_minimum_does_not_create_playlist(self):
        self.cfg["minimum_channels"] = 100
        self.prepare([{"id": "A.ru", "name": "A", "group": "ТВ"}], '#EXTM3U\n#EXTINF:-1 tvg-id="A.ru",A\nhttps://example.com/a.m3u8\n')
        r = self.build(["https://example.com/a.m3u8"])
        self.assertEqual(r["publication"], "blocked")
        self.assertFalse((self.root / "tv.m3u").exists())

    def test_publish_exactly_100_unique_passed_channels(self):
        self.cfg["minimum_channels"] = 100
        channels = [{"id": f"C{i}.ru", "name": f"Channel {i}", "group": "ТВ"} for i in range(100)]
        urls = [f"https://example.com/{i}.m3u8" for i in range(100)]
        text = "#EXTM3U\n" + "".join(f'#EXTINF:-1 tvg-id="C{i}.ru",Channel {i}\n{u}\n' for i, u in enumerate(urls))
        self.prepare(channels, text)
        r = self.build(urls)
        self.assertEqual(r["publication"], "published")
        self.assertEqual(r["unique_channels_passed"], 100)
        self.assertEqual(len(sv.parse_m3u((self.root / "tv.m3u").read_text())), 100)
        self.assertEqual(r["stable_channels"], 0)

    def test_catalog_duplicate_names_abort(self):
        self.prepare([{"id": "A.ru", "name": "НТВ HD", "group": "ТВ"}, {"id": "B.ru", "name": "НТВ", "group": "ТВ"}], '#EXTM3U\n')
        with self.assertRaises(ValueError):
            self.build()

    def test_master_url_is_kept_and_cdn_aliases_are_deduplicated(self):
        self.prepare([{"id": "A.ru", "name": "A", "group": "ТВ"}, {"id": "B.ru", "name": "B", "group": "ТВ"}], '''#EXTM3U
#EXTINF:-1 tvg-id="A.ru",A
https://example.com/master-a.m3u8
#EXTINF:-1 tvg-id="B.ru",B
https://example.com/master-b.m3u8
''')
        def checker(item, cfg):
            return {"ok": True, "reason": "ok", "media_url": "https://cdn.example.com/same.m3u8?expires=123"}
        with contextlib.redirect_stdout(io.StringIO()):
            r = sv.build(self.cfg, tester=checker)
        self.assertEqual(r["unique_channels_passed"], 1)
        text = (self.root / "tv.m3u").read_text()
        self.assertIn("master-a.m3u8", text)
        self.assertNotIn("expires=123", text)

    def test_api_source_keeps_labels_and_correct_feed_identity(self):
        self.prepare([{"id": "A.ru", "name": "A", "group": "ТВ"}], '#EXTM3U\n')
        payload = json.dumps([{"channel": "A.ru", "feed": "HD", "title": "A HD",
                              "url": "https://example.com/a.m3u8", "labels": ["Geo-blocked"],
                              "user_agent": "Agent (test, comma)"}]).encode()
        with patch.object(sv, "fetch_bytes", return_value=payload):
            records, reports = sv.load_sources({"sources": [{"url": "https://example.com/streams.json", "format": "iptv-org-streams"}]})
        self.assertEqual(records[0]["attrs"]["tvg-id"], "A.ru@HD")
        self.assertIn("[Geo-blocked]", records[0]["name"])
        self.assertEqual(records[0]["headers"]["User-Agent"], "Agent (test, comma)")


class ParserAndHistoryCase(unittest.TestCase):
    def test_part_time_label_cannot_be_lost_in_another_catalog(self):
        items = sv.parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="A.ru",A [Not 24/7]\nhttps://example.com/a.m3u8\n#EXTINF:-1 tvg-id="A.ru",A\nhttps://example.com/a.m3u8\n')
        found, _ = sv.group_candidates(items, [{'id':'A.ru','name':'A','group':'ТВ'}], {})
        self.assertEqual(found['a.ru'], [])

    def test_short_live_window_accumulates_required_playback(self):
        def playlist(sequence):
            return f'#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n#EXTINF:3,\n{sequence}.ts\n'
        sample = {"pixels": 160 * 90, "played_seconds": 3, "download_seconds": .2}
        with patch.object(sv, "choose_media", return_value=('https://example.com/a.m3u8', playlist(1), None)), \
             patch.object(sv, "manifest", side_effect=[playlist(2), playlist(3), playlist(4)]), \
             patch.object(sv, "decode_sample", return_value=sample) as decode, \
             patch.object(sv.time, "sleep"):
            r = sv.check_stream({"url": "https://example.com/a.m3u8"}, {"test_seconds": 12})
        self.assertTrue(r["ok"])
        self.assertEqual(r["played_seconds"], 12)
        self.assertEqual(decode.call_count, 4)

    def test_short_live_window_without_new_media_fails(self):
        playlist = '#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:3,\n1.ts\n'
        sample = {"pixels": 160 * 90, "played_seconds": 3, "download_seconds": .2}
        with patch.object(sv, "choose_media", return_value=('https://example.com/a.m3u8', playlist, None)), \
             patch.object(sv, "manifest", return_value=playlist), \
             patch.object(sv, "decode_sample", return_value=sample), \
             patch.object(sv.time, "sleep"):
            r = sv.check_stream({"url": "https://example.com/a.m3u8"}, {"test_seconds": 12})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], 'live manifest did not advance')

    def test_regional_channel_is_not_silently_mapped_to_national(self):
        item = sv.parse_m3u('#EXTM3U\n#EXTINF:-1 tvg-id="local8",8 канал (Красноярск)\nhttps://example.com/local.m3u8\n')[0]
        item["match_by_name"] = True
        found, _ = sv.group_candidates([item], [{"id": "Channel8.ru", "name": "8 канал", "group": "ТВ"}], {})
        self.assertEqual(found["channel8.ru"], [])

    def test_separate_audio_rendition_chooses_russian_track(self):
        master = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="en",DEFAULT=YES,URI="en.m3u8"\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="ru",DEFAULT=NO,URI="ru.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,AUDIO="a"\nvideo.m3u8\n'
        media = '#EXTM3U\n#EXTINF:6,\n1.ts\n'
        with patch.object(sv, "manifest", side_effect=[master, media]):
            url, text, audio = sv.choose_media('https://example.com/master.m3u8', {}, {})
        self.assertEqual(url, 'https://example.com/video.m3u8')
        self.assertEqual(audio, 'https://example.com/ru.m3u8')

    def test_explicit_source_id_mapping_does_not_map_timeshift(self):
        text = '#EXTM3U\n#EXTINF:-1 tvg-id="one",Первый\nhttps://example.com/main.m3u8\n#EXTINF:-1 tvg-id="one-pl2",Первый +2\nhttps://example.com/plus2.m3u8\n'
        with patch.object(sv, "fetch_bytes", return_value=text.encode()):
            items, _ = sv.load_sources({"sources": [{"url": "https://example.com/list.m3u", "id_map": {"one": "ChannelOne.ru"}}]})
        found, _ = sv.group_candidates(items, [{"id": "ChannelOne.ru", "name": "Первый канал", "group": "ТВ"}], {})
        self.assertEqual([x["url"] for x in found["channelone.ru"]], ['https://example.com/main.m3u8'])

    def test_quoted_vlc_user_agent_loses_only_outer_quotes(self):
        p = sv.parse_m3u('#EXTM3U\n#EXTINF:-1,A\n#EXTVLCOPT:http-user-agent="WINK/1.40 (AndroidTV/9)"\nhttps://example.com/a.m3u8\n')[0]
        self.assertEqual(p["headers"]["User-Agent"], "WINK/1.40 (AndroidTV/9)")

    def test_comma_in_header_is_not_channel_separator(self):
        p = sv.parse_m3u('''#EXTM3U
#EXTINF:-1 tvg-id="NTV.ru@HD" http-user-agent="Mozilla (KHTML, like Gecko)" group-title="Новости, ТВ",НТВ, эфир
https://example.com/ntv.m3u8
''')[0]
        self.assertEqual(p["name"], "НТВ, эфир")
        self.assertEqual(p["headers"]["User-Agent"], "Mozilla (KHTML, like Gecko)")

    def test_private_and_file_urls_excluded(self):
        for url in ["file:///etc/passwd", "http://127.0.0.1/a", "http://192.168.1.1/a"]:
            self.assertEqual(sv.parse_m3u(f'#EXTM3U\n#EXTINF:-1,A\n{url}\n'), [])

    def test_repeated_runs_do_not_fake_history(self):
        h = {}
        for minute in range(12):
            h = sv.observation(h, {"ok": True}, f"2026-10-27T00:{minute:02}:00+00:00", {})
        self.assertEqual(h["samples"], 1)
        self.assertFalse(h["stable"])

    def test_quick_retry_cannot_erase_failure_in_same_interval(self):
        h = sv.observation({}, {"ok": False}, "2026-10-27T00:00:00+00:00", {})
        h = sv.observation(h, {"ok": True}, "2026-10-27T00:10:00+00:00", {})
        self.assertEqual(h["samples"], 1)
        self.assertEqual(h["success_rate"], 0)
        self.assertFalse(h["checks"][0]["ok"])
        self.assertTrue(h["last_result"]["ok"])

    def test_maturity_needs_time_and_success(self):
        from datetime import datetime, timedelta, timezone
        h = {}
        start = datetime(2026, 10, 24, tzinfo=timezone.utc)
        for i in range(8):
            h = sv.observation(h, {"ok": True}, (start + timedelta(hours=i * 6)).isoformat(), {})
        self.assertTrue(h["stable"])
        h = sv.observation(h, {"ok": False}, (start + timedelta(hours=48)).isoformat(), {})
        self.assertFalse(h["stable"])

    def test_old_unstamped_v2_checks_not_used_as_new_evidence(self):
        h = sv.observation({"checks": [1] * 28}, {"ok": True}, "2026-10-27T00:00:00+00:00", {})
        self.assertEqual(h["samples"], 1)
        self.assertFalse(h["stable"])

    def test_media_sequences_and_relative_segments(self):
        p = sv.media_segments('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:8\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts\n#EXTINF:6,\nb.ts\n', 'https://example.com/live/index.m3u8')
        self.assertEqual([s["sequence"] for s in p["segments"]], [8, 9])
        self.assertEqual(p["segments"][-1]["url"], 'https://example.com/live/b.ts')

    def test_vod_and_encryption_are_not_live_candidates(self):
        for extra in ['#EXT-X-ENDLIST', '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"']:
            with self.assertRaises(ValueError):
                sv.media_segments(f'#EXTM3U\n{extra}\n#EXTINF:6,\na.ts\n', 'https://example.com/a.m3u8')


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class DecodeIntegrationCase(unittest.TestCase):
    def fixture(self, path, audio=True):
        args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        args += ["-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-threads", "1"]
        if audio:
            args += ["-c:a", "aac"]
        subprocess.run(args + ["-f", "mpegts", str(path)], check=True, capture_output=True, timeout=15)

    def test_actual_video_and_audio_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.ts"
            self.fixture(path)
            with patch.object(sv, "fetch_bytes", return_value=path.read_bytes()):
                r = sv.decode_sample([{"url": "https://example.com/a.ts", "seconds": 2}], {}, {})
            self.assertGreaterEqual(r["played_seconds"], 1.8)
            self.assertEqual(r["pixels"], 160 * 90)

    def test_video_without_audio_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.ts"
            self.fixture(path, audio=False)
            with patch.object(sv, "fetch_bytes", return_value=path.read_bytes()):
                with self.assertRaisesRegex(ValueError, "missing video or audio"):
                    sv.decode_sample([{"url": "https://example.com/a.ts", "seconds": 2}], {}, {})

    def test_separate_video_and_audio_are_really_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / 'video.ts'
            audio = Path(tmp) / 'audio.ts'
            self.fixture(video, audio=False)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                            '-t', '2', '-c:a', 'aac', '-f', 'mpegts', str(audio)],
                           check=True, capture_output=True, timeout=15)
            def fetch(url, *args):
                return audio.read_bytes() if url.endswith('audio.ts') else video.read_bytes()
            with patch.object(sv, 'fetch_bytes', side_effect=fetch):
                r = sv.decode_sample([{'url':'https://example.com/video.ts','seconds':2}], {}, {},
                                     [{'url':'https://example.com/audio.ts','seconds':2}])
            self.assertGreaterEqual(r['played_seconds'], 1.8)
            self.assertEqual(r['pixels'], 160 * 90)


if __name__ == "__main__":
    unittest.main()
