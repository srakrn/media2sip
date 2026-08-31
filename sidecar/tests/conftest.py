"""Sidecar test fixtures.

These run inside the sidecar image, where ffmpeg and pjsua2 exist. The SIP stack
itself is not exercised here - that needs a registrar, and is the job of the
Asterisk-in-CI suite. What is exercised is everything around it: media
resolution, the cache, the event bus, and the control API's contract.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def sounds_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sounds"
    d.mkdir()
    return d


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture
def make_wav():
    """Build a real WAV with ffmpeg. Synthetic bytes would not exercise the
    transcode path, which is the part that actually breaks."""

    def _make(path: Path, seconds: float = 1.0, rate: int = 44100, freq: int = 440) -> Path:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}",
             "-ac", "2", str(path)],
            check=True,
        )
        return path

    return _make
