"""Conservative matching of visually reviewed denial/test-screen frames.

Only the sampled frame is classified; no URL or channel is permanently banned.
Standard library + the FFmpeg already required by SV IPTV.
"""
from __future__ import annotations
import base64
import json
import math
from pathlib import Path
import subprocess
from functools import lru_cache

WIDTH, HEIGHT = 96, 54


def frame_vector(path: Path) -> bytes:
    run = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1",
                          "-i", str(path), "-frames:v", "1", "-vf", f"scale={WIDTH}:{HEIGHT},format=gray",
                          "-threads", "1", "-f", "rawvideo", "pipe:1"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode or len(run.stdout) != WIDTH * HEIGHT:
        raise ValueError("Не удалось проверить кадр на известную заглушку")
    return run.stdout


@lru_cache(maxsize=4)
def templates(path: str, modified_ns: int) -> tuple:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != 1 or data.get("size") != [WIDTH, HEIGHT]:
        raise ValueError("Повреждён набор образцов заглушек")
    out = []
    for item in data["templates"]:
        vector = base64.b64decode(item["gray_base64"], validate=True)
        if len(vector) != WIDTH * HEIGHT:
            raise ValueError("Неверный размер образца заглушки")
        out.append((item["id"], item["reason"], vector))
    return tuple(out)


def match_vector(vector: bytes, references: tuple) -> dict | None:
    if len(vector) != WIDTH * HEIGHT:
        raise ValueError("Неверный размер кадра")
    # A dark image alone must never be mistaken for a denial screen. Require
    # near-identical pixel structure, contrast, and brightness, including text.
    n = len(vector); mean = sum(vector) / n
    centered = [v - mean for v in vector]
    variance = sum(v * v for v in centered)
    if variance < n * 4:
        return None
    for name, reason, ref in references:
        mae = sum(abs(a-b) for a,b in zip(vector, ref)) / n
        if mae > 2.0:
            continue
        ref_mean = sum(ref) / n
        ref_centered = [v-ref_mean for v in ref]
        ref_variance = sum(v*v for v in ref_centered)
        if not ref_variance or not .85 <= variance / ref_variance <= 1.18:
            continue
        correlation = sum(a*b for a,b in zip(centered,ref_centered)) / math.sqrt(variance*ref_variance)
        if correlation >= .99:
            return {"id": name, "reason": reason, "mean_pixel_difference": round(mae,4),
                    "correlation": round(correlation,6)}
    return None


def inspect(path: Path, root: Path) -> dict:
    source = root / "placeholder-templates.json"
    if not path.exists():
        return {"state": "no_frame"}
    if not source.exists():
        return {"state": "no_templates"}
    try:
        match = match_vector(frame_vector(path), templates(str(source), source.stat().st_mtime_ns))
        return {"state": "blocked_frame", **match} if match else {"state": "no_known_placeholder"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"state": "inspection_failed", "error": str(exc)[:160]}
