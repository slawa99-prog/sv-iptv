#!/usr/bin/env python3
"""Run SV IPTV on a Russian VPS with separate state and report publication."""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, HTTPRedirectHandler, build_opener

REPOSITORY = "slawa99-prog/sv-iptv"
SOURCE_URL = f"https://github.com/{REPOSITORY}.git"
REPORT_BRANCH = "ru-checks"
REPORT_FILES = ("tv.m3u", "status.json", "history.json", "backups.json", "node.json")
ENVIRONMENT = {"kind": "sv-iptv-russian-vps", "version": 1}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        temporary = Path(f.name)
    temporary.replace(path)


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *args], text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=180,
    ).strip()


def refresh_source(source: Path) -> str:
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", "main", SOURCE_URL, str(source)],
            check=True, timeout=180, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    else:
        if git(source, "remote", "get-url", "origin") != SOURCE_URL:
            raise ValueError("Unexpected source repository; refusing to replace its checkout")
        git(source, "fetch", "--depth", "1", "origin", "main")
        git(source, "-c", "advice.detachedHead=false", "checkout", "--detach", "--force", "FETCH_HEAD")
    return git(source, "rev-parse", "HEAD")


def prepare_workspace(source: Path, output: Path, workers: int) -> dict:
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    output.mkdir(parents=True, exist_ok=True)
    marker = output / "environment.json"
    if marker.exists():
        if json.loads(marker.read_text()) != ENVIRONMENT:
            raise ValueError("This directory belongs to a different measurement environment")
    elif any((output / name).exists() for name in REPORT_FILES):
        raise ValueError("Existing unlabelled results must not be imported as Russian VPS history")
    write_json(marker, ENVIRONMENT)
    cfg = json.loads((source / "config.json").read_text(encoding="utf-8"))
    scope = cfg.get("channel_scope", {})
    if scope.get("mode") != "reviewed-allowlist" or not scope.get("allowed_ids"):
        raise ValueError("A reviewed language allowlist is required")
    inputs = {"channels.json", "channel-groups.json"}
    for entry in cfg.get("sources", []):
        url = entry if isinstance(entry, str) else entry["url"]
        if not url.startswith(("https://", "http://")):
            inputs.add(url)
    for name in inputs:
        if Path(name).is_absolute():
            raise ValueError("Local catalog path must be relative")
        candidate = (source / name).resolve()
        if not candidate.is_relative_to(source.resolve()):
            raise ValueError("Local catalog path escapes the source directory")
        target = output / name
        if not target.resolve().is_relative_to(output.resolve()):
            raise ValueError("Local catalog path escapes the results directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, target)
    # Source catalogs are reusable; observations from other networks are not.
    for seed in (source / "seed").glob("*.json"):
        target = output / "seed" / seed.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(seed, target)
    cfg.update(
        publication_mode="automatic", check_environment="ru-vps",
        workers=workers, per_host_workers=1, skip_tagged_geo_blocked=False,
        resume_checks=True,
    )
    write_json(output / "config.json", cfg)
    return cfg


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token: str):
        self.token = token
        self.opener = build_opener(NoRedirect)

    def call(self, method: str, path: str, data: dict | None = None):
        request = Request(
            f"https://api.github.com/repos/{REPOSITORY}/{path}",
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2026-03-10",
                     "User-Agent": "SV-IPTV-Russian-Checker"},
        )
        with self.opener.open(request, timeout=90) as response:
            return json.load(response)


def publish_report(output: Path, api) -> str:
    """Publish only completed reports to the dedicated branch; never write main."""
    for name in ("status.json", "history.json", "node.json"):
        if not (output / name).is_file():
            raise ValueError(f"Completed report is missing {name}")
    status = json.loads((output / "status.json").read_text())
    node = json.loads((output / "node.json").read_text())
    if status.get("check_environment") != "ru-vps" or node.get("checked_utc") != status.get("checked_utc"):
        raise ValueError("Missing or mismatched Russian VPS observation")
    if status.get("publication") not in ("published", "blocked"):
        raise ValueError("Incomplete check result")
    if status["publication"] == "published" and not (output / "tv.m3u").exists():
        raise ValueError("Published check has no playlist")
    try:
        head = api("GET", f"git/ref/heads/{REPORT_BRANCH}")["object"]["sha"]
    except HTTPError as exc:
        if exc.code != 404:
            raise
        head = None
    entries = []
    for name in REPORT_FILES:
        path = output / name
        if path.exists():
            if path.stat().st_size > 45 * 1024 * 1024:
                raise ValueError(f"Report file too large: {name}")
            blob = api("POST", "git/blobs", {"content": path.read_text(encoding="utf-8"), "encoding": "utf-8"})
            entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    readme = (
        "# SV IPTV — проверка российского VPS\n\n"
        "Это независимые результаты узла в России. Основной `main/tv.m3u` не меняется.\n\n"
        f"Последний завершённый проход (UTC): {status['checked_utc']}.\n\n"
        f"Каналов прошло проверку: {status.get('unique_channels_passed', 0)}. "
        f"Результат сборки: {status['publication']}.\n\n"
        "При `blocked` сохранённая M3U может относиться к предыдущему проходу: "
        "проверяйте `last_published_utc` и `publication_reason` в status.json.\n"
    )
    entries.append({"path": "README.md", "mode": "100644", "type": "blob", "content": readme})
    # Own report-only tree: no code, secrets, or observations from main.
    tree = api("POST", "git/trees", {"tree": entries})["sha"]
    commit = api("POST", "git/commits", {
        "message": f"Russian VPS checks: {status['checked_utc']}",
        "tree": tree, "parents": [head] if head else [],
    })["sha"]
    if head:
        api("PATCH", f"git/refs/heads/{REPORT_BRANCH}", {"sha": commit, "force": False})
    else:
        api("POST", "git/refs", {"ref": f"refs/heads/{REPORT_BRANCH}", "sha": commit})
    return commit


def credential() -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return ""
    path = Path(directory) / "github-token"
    return path.read_text().strip() if path.is_file() else ""


def run(state: Path, workers: int) -> int:
    source, output = state / "source", state / "results"
    revision = refresh_source(source)
    cfg = prepare_workspace(source, output, workers)
    sys.path.insert(0, str(source))
    checker = importlib.import_module("sv_iptv")
    # Validation templates stay in the source checkout; measurement state is separate.
    checker.ROOT = output
    checker.VALIDATION_ROOT = source
    report = checker.build(cfg)
    write_json(output / "node.json", {
        "environment": "ru-vps", "location": "Moscow, Russia (operator configured)",
        "source_commit": revision, "checked_utc": report["checked_utc"],
        "workers": workers, "main_playlist_modified": False,
    })
    token = credential()
    if token:
        commit = publish_report(output, GitHub(token).call)
        write_json(state / "publication.json", {"branch": REPORT_BRANCH, "commit": commit,
                   "checked_utc": report["checked_utc"]})
        print(f"Russian report saved to GitHub branch {REPORT_BRANCH}: {commit}", flush=True)
    else:
        print(f"Results saved locally: {output}; GitHub publication is not configured.", flush=True)
    return 2 if report["publication"] == "blocked" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/sv-iptv-ru"))
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    with (args.state / "check.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A Russian VPS check is already running; skipped.")
            return 0
        try:
            return run(args.state, args.workers)
        except HTTPError as exc:
            print(f"ERROR: GitHub/network HTTP {exc.code}; local results retained", file=sys.stderr)
            return 1
        except Exception as exc:
            # Do not dump request objects, credentials, or remote response bodies.
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
