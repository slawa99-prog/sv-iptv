import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from server import ru_check as ru


class FakeGitHub:
    def __init__(self, head=None, conflict=False):
        self.head, self.conflict, self.calls = head, conflict, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            if self.head is None:
                raise HTTPError("https://api.github.com", 404, "Not found", {}, None)
            return {"object": {"sha": self.head}}
        if method == "PATCH" and self.conflict:
            raise HTTPError("https://api.github.com", 422, "Not fast forward", {}, None)
        return {"sha": "a" * 40}


class RussianVPSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.output = self.root / "source", self.root / "results"
        self.source.mkdir()
        self.config = {
            "sources": ["custom.m3u", {"url": "expansion-candidates.m3u"},
                        {"url": "https://example.com/list.m3u"}],
            "channel_scope": {"mode": "reviewed-allowlist", "allowed_ids": ["Russian.ru"]},
            "minimum_channels": 100, "minimum_retained_fraction": .75,
            "test_seconds": 12, "stable_min_samples": 8, "stable_min_hours": 42,
        }
        ru.write_json(self.source / "config.json", self.config)
        ru.write_json(self.source / "channels.json", {"fixture": True})
        ru.write_json(self.source / "channel-groups.json", {"fixture": True})
        ru.write_json(self.source / "history.json", {"network": "foreign"})
        (self.source / "custom.m3u").write_text("#EXTM3U\n")
        (self.source / "expansion-candidates.m3u").write_text("#EXTM3U\n#expansion\n")

    def report(self, publication="published"):
        self.output.mkdir(exist_ok=True)
        ru.write_json(self.output / "status.json", {"check_environment": "ru-vps",
            "checked_utc": "2026-11-11T15:00:00+00:00", "publication": publication,
            "unique_channels_passed": 110})
        ru.write_json(self.output / "node.json", {"checked_utc": "2026-11-11T15:00:00+00:00"})
        ru.write_json(self.output / "history.json", {"network": "ru-vps"})
        (self.output / "tv.m3u").write_text("#EXTM3U\n")

    def test_separate_history_full_inputs_language_filter_and_strict_checks(self):
        cfg = ru.prepare_workspace(self.source, self.output, 2)
        self.assertFalse((self.output / "history.json").exists())
        self.assertEqual((self.output / "expansion-candidates.m3u").read_text(), "#EXTM3U\n#expansion\n")
        for key in ("channel_scope", "minimum_channels", "minimum_retained_fraction",
                    "test_seconds", "stable_min_samples", "stable_min_hours"):
            self.assertEqual(cfg[key], self.config[key])
        self.assertEqual((cfg["check_environment"], cfg["workers"]), ("ru-vps", 2))
        self.assertFalse(cfg["skip_tagged_geo_blocked"])
        ru.write_json(self.output / "history.json", {"network": "ru-vps", "cycles": 3})
        ru.prepare_workspace(self.source, self.output, 2)
        self.assertEqual(json.loads((self.output / "history.json").read_text())["cycles"], 3)
        self.assertEqual(json.loads((self.source / "history.json").read_text())["network"], "foreign")

    def test_unlabelled_results_are_rejected(self):
        self.report()
        with self.assertRaises(ValueError):
            ru.prepare_workspace(self.source, self.output, 2)

    def test_allowlist_required_and_catalog_cannot_escape(self):
        for mutation in ({"channel_scope": {}}, {"sources": ["../config.json"]},
                         {"sources": [str(self.source / "custom.m3u")]}):
            with self.subTest(mutation=mutation):
                ru.write_json(self.source / "config.json", {**self.config, **mutation})
                with self.assertRaises(ValueError):
                    ru.prepare_workspace(self.source, self.output, 2)

    def test_publish_creates_only_report_branch_and_excludes_secrets(self):
        self.report()
        (self.output / "github-token").write_text("must-not-be-published")
        api = FakeGitHub()
        ru.publish_report(self.output, api)
        ref = next(body for method, path, body in api.calls if path == "git/refs")
        self.assertEqual(ref["ref"], "refs/heads/ru-checks")
        tree = next(body for method, path, body in api.calls if path == "git/trees")
        self.assertEqual({e["path"] for e in tree["tree"]},
                         {"tv.m3u", "status.json", "history.json", "node.json", "README.md"})
        self.assertNotIn("must-not-be-published", json.dumps(api.calls))
        self.assertFalse(any("heads/main" in path for _, path, _ in api.calls))

    def test_existing_branch_is_updated_without_force_and_conflict_is_not_retried(self):
        self.report()
        api = FakeGitHub(head="b" * 40, conflict=True)
        with self.assertRaises(HTTPError):
            ru.publish_report(self.output, api)
        patches = [(path, body) for method, path, body in api.calls if method == "PATCH"]
        self.assertEqual(patches, [("git/refs/heads/ru-checks", {"sha": "a" * 40, "force": False})])
        self.assertTrue((self.output / "history.json").exists())

    def test_foreign_or_mismatched_result_is_never_published(self):
        self.report()
        for mutation in ({"check_environment": "github-actions"}, {"checked_utc": "old"}):
            with self.subTest(mutation=mutation):
                self.report()
                status = json.loads((self.output / "status.json").read_text())
                ru.write_json(self.output / "status.json", {**status, **mutation})
                api = FakeGitHub()
                with self.assertRaises(ValueError):
                    ru.publish_report(self.output, api)
                self.assertEqual(api.calls, [])

    def test_blocked_first_run_can_report_without_a_playlist(self):
        self.report("blocked")
        (self.output / "tv.m3u").unlink()
        api = FakeGitHub()
        ru.publish_report(self.output, api)
        tree = next(body for method, path, body in api.calls if path == "git/trees")
        self.assertNotIn("tv.m3u", {entry["path"] for entry in tree["tree"]})
        self.assertIn("blocked", next(entry["content"] for entry in tree["tree"] if entry["path"] == "README.md"))


if __name__ == "__main__":
    unittest.main()
