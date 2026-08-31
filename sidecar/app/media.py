"""Media resolution: turn a request into a WAV pjsua2 can play, and cache it.

Everything the PBX will accept here is narrowband (phase 1 measured PCMU 8 kHz
mono), so every clip is normalised to 16-bit PCM mono at the configured rate.
pjsua2's AudioMediaPlayer wants a real WAV on disk, which is also what makes the
cache worth having: Home Assistant already caches TTS by message hash, so a
repeated phrase skips both the fetch and the transcode.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

_LOGGER = logging.getLogger(__name__)

SOUND_PREFIX = "sound:"


class MediaError(Exception):
    """Raised when a clip cannot be produced. Always surfaced to the caller."""


@dataclass(frozen=True)
class Clip:
    path: Path
    duration: float
    cached: bool
    source: str

    @property
    def label(self) -> str:
        """A short name for logs and diagnostics.

        Never the raw URL. Home Assistant's TTS proxy URLs carry a token that
        grants access to the audio, and diagnostics downloads get shared around.
        The content hash is what you actually want anyway: it answers "was this
        the same clip as last time".
        """
        if self.source.startswith(("http://", "https://")):
            host = urlsplit(self.source).netloc
            return f"url({host}) {self.path.stem}"
        return self.source or self.path.stem


class MediaResolver:
    def __init__(
        self,
        cache_dir: Path,
        sounds_dir: Path,
        sample_rate: int = 8000,
        cache_max_bytes: int = 256 * 1024 * 1024,
        fetch_timeout: float = 20.0,
        max_bytes: int = 32 * 1024 * 1024,
        builtin_dir: Path | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.sounds_dir = sounds_dir
        # Sounds shipped in the image, searched after the user's own so they can
        # be overridden by name. This exists because a Home Assistant add-on gets
        # /data mounted as its persistent volume, which would otherwise shadow
        # everything the image baked in there.
        self.builtin_dir = builtin_dir
        self.sample_rate = sample_rate
        self.cache_max_bytes = cache_max_bytes
        self.fetch_timeout = fetch_timeout
        self.max_bytes = max_bytes
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    # -- startup ---------------------------------------------------------

    @staticmethod
    async def assert_ffmpeg() -> None:
        """Fail loudly at startup rather than silently at page time."""
        for tool in ("ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                raise MediaError(f"{tool} not found on PATH; the sidecar image is broken")

    # -- public ----------------------------------------------------------

    def _sound_dirs(self) -> list[Path]:
        dirs = [self.sounds_dir]
        if self.builtin_dir is not None and self.builtin_dir != self.sounds_dir:
            dirs.append(self.builtin_dir)
        return [d for d in dirs if d.is_dir()]

    def list_sounds(self) -> list[str]:
        return sorted({p.stem for d in self._sound_dirs() for p in d.glob("*.wav")})

    async def resolve(self, media: str, headers: dict[str, str] | None = None) -> Clip:
        """Resolve `sound:name`, an http(s) URL, or a local path to a playable clip."""
        if media.startswith(SOUND_PREFIX):
            return await self._resolve_sound(media[len(SOUND_PREFIX):])
        if media.startswith(("http://", "https://")):
            return await self._resolve_url(media, headers or {})
        return await self._resolve_path(Path(media))

    async def concat(self, clips: list[Clip]) -> Clip:
        """Join clips (a chime then an announcement) into one playable file.

        Done as a file rather than by playing two players back to back, because a
        gap between players is audible on a page and risks the tail being cut.
        """
        clips = [c for c in clips if c is not None]
        if not clips:
            raise MediaError("nothing to play")
        if len(clips) == 1:
            return clips[0]

        digest = hashlib.sha256("|".join(str(c.path) for c in clips).encode()).hexdigest()[:32]
        out = self.cache_dir / f"concat-{digest}-{self.sample_rate}.wav"
        async with self._lock(out.name):
            if out.is_file():
                return Clip(out, await self._duration(out), True, "concat")
            listing = self.cache_dir / f"concat-{digest}.txt"
            listing.write_text("".join(f"file '{c.path}'\n" for c in clips))
            await self._run(
                "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                "-i", str(listing), *self._encode_args(), str(out),
            )
            listing.unlink(missing_ok=True)
        return Clip(out, await self._duration(out), False, "concat")

    # -- resolution ------------------------------------------------------

    async def _resolve_sound(self, name: str) -> Clip:
        if "/" in name or name in {"", ".", ".."}:
            raise MediaError(f"bad sound name: {name!r}")
        for directory in self._sound_dirs():
            for candidate in (directory / f"{name}.wav", directory / name):
                if candidate.is_file():
                    return await self._transcode(
                        candidate, key=f"sound-{name}", source=f"sound:{name}"
                    )
        raise MediaError(f"no such sound: {name!r} (have: {', '.join(self.list_sounds()) or 'none'})")

    async def _resolve_path(self, path: Path) -> Clip:
        if not path.is_file():
            raise MediaError(f"no such file: {path}")
        stat = path.stat()
        key = hashlib.sha256(f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()[:32]
        return await self._transcode(path, key=f"file-{key}", source=str(path))

    async def _resolve_url(self, url: str, headers: dict[str, str]) -> Clip:
        """Fetch, then transcode, caching on the hash of the *content*.

        Hashing content rather than URL matters because Home Assistant's TTS URLs
        carry a per-request token, so the same phrase arrives under a new URL
        every time and a URL-keyed cache would never hit.
        """
        raw = await self._fetch(url, headers)
        key = f"url-{hashlib.sha256(raw).hexdigest()[:32]}"
        target = self.cache_dir / f"{key}-{self.sample_rate}.wav"

        async with self._lock(target.name):
            if target.is_file():
                target.touch()
                return Clip(target, await self._duration(target), True, url)
            staging = self.cache_dir / f".{key}.download"
            staging.write_bytes(raw)
            try:
                await self._transcode_to(staging, target)
            finally:
                staging.unlink(missing_ok=True)
        await self._evict()
        return Clip(target, await self._duration(target), False, url)

    async def _fetch(self, url: str, headers: dict[str, str]) -> bytes:
        try:
            async with httpx.AsyncClient(timeout=self.fetch_timeout, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    chunks, total = [], 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise MediaError(f"media at {url} exceeds {self.max_bytes} bytes")
                        chunks.append(chunk)
        except httpx.HTTPError as err:
            raise MediaError(f"cannot fetch {url}: {err}") from err
        if not chunks:
            raise MediaError(f"empty media at {url}")
        return b"".join(chunks)

    async def _transcode(self, src: Path, key: str, source: str) -> Clip:
        target = self.cache_dir / f"{key}-{self.sample_rate}.wav"
        async with self._lock(target.name):
            if target.is_file() and target.stat().st_mtime >= src.stat().st_mtime:
                return Clip(target, await self._duration(target), True, source)
            await self._transcode_to(src, target)
        return Clip(target, await self._duration(target), False, source)

    async def _transcode_to(self, src: Path, target: Path) -> None:
        tmp = target.with_suffix(".tmp.wav")
        await self._run("ffmpeg", "-y", "-v", "error", "-i", str(src), *self._encode_args(), str(tmp))
        tmp.replace(target)  # atomic, so a concurrent reader never sees a half file

    def _encode_args(self) -> tuple[str, ...]:
        return ("-ac", "1", "-ar", str(self.sample_rate), "-acodec", "pcm_s16le", "-f", "wav")

    async def _duration(self, path: Path) -> float:
        out = await self._run(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        )
        try:
            return float(out.strip())
        except ValueError as err:
            raise MediaError(f"cannot read duration of {path}") from err

    # -- plumbing --------------------------------------------------------

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    @staticmethod
    async def _run(*args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise MediaError(f"{args[0]} failed: {stderr.decode(errors='replace').strip()[:400]}")
        return stdout.decode(errors="replace")

    async def _evict(self) -> None:
        """Trim the cache to its byte budget, oldest first."""
        files = sorted(self.cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while total > self.cache_max_bytes and files:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
            _LOGGER.info("evicted %s from the media cache", victim.name)
