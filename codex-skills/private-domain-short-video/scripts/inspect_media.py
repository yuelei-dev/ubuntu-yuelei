#!/usr/bin/env python3
"""Inspect one or more media files with ffprobe and emit compact JSON."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def probe(path: Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
    data = json.loads(completed.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    width = video.get("width") if video else None
    height = video.get("height") if video else None
    orientation = None
    if width and height:
        orientation = "vertical" if height > width else "landscape" if width > height else "square"
    return {
        "path": str(path.resolve()),
        "exists": True,
        "duration_seconds": round(float(data.get("format", {}).get("duration", 0.0)), 3),
        "size_bytes": int(data.get("format", {}).get("size", 0)),
        "video": video,
        "audio": audio,
        "orientation": orientation,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is required but was not found on PATH")
    results = []
    for path in args.files:
        if not path.is_file():
            results.append({"path": str(path), "exists": False, "error": "not a file"})
            continue
        try:
            results.append(probe(path))
        except subprocess.CalledProcessError as exc:
            results.append({"path": str(path.resolve()), "exists": True, "error": exc.stderr.strip()})
    # ASCII escapes keep Windows shell output machine-readable across code pages.
    print(json.dumps(results, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
