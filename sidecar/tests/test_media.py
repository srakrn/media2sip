"""Media resolution, transcoding and the content-hash cache."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.media import MediaError, MediaResolver


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=sample_rate,channels,codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    codec, rate, channels = out.split(",")
    return {"codec": codec, "rate": int(rate), "channels": int(channels)}


@pytest.fixture
def resolver(cache_dir: Path, sounds_dir: Path) -> MediaResolver:
    return MediaResolver(cache_dir=cache_dir, sounds_dir=sounds_dir, sample_rate=8000)


async def test_ffmpeg_is_present() -> None:
    """Detect a broken image at startup, not at page time."""
    await MediaResolver.assert_ffmpeg()


async def test_sound_is_transcoded_to_narrowband(
    resolver: MediaResolver, sounds_dir: Path, make_wav
) -> None:
    """Phase 1 measured PCMU 8 kHz mono; everything is normalised to match."""
    make_wav(sounds_dir / "chime.wav", seconds=1.0, rate=44100)

    clip = await resolver.resolve("sound:chime")

    assert _probe(clip.path) == {"codec": "pcm_s16le", "rate": 8000, "channels": 1}
    assert clip.duration == pytest.approx(1.0, abs=0.05)
    assert not clip.cached


async def test_second_resolve_hits_the_cache(
    resolver: MediaResolver, sounds_dir: Path, make_wav
) -> None:
    make_wav(sounds_dir / "chime.wav")
    first = await resolver.resolve("sound:chime")
    second = await resolver.resolve("sound:chime")

    assert not first.cached
    assert second.cached
    assert second.path == first.path


async def test_missing_sound_is_a_clear_error(resolver: MediaResolver) -> None:
    with pytest.raises(MediaError, match="no such sound"):
        await resolver.resolve("sound:nope")


@pytest.mark.parametrize("name", ["../etc/passwd", "..", ".", "", "sub/dir"])
async def test_sound_names_cannot_escape_the_volume(
    resolver: MediaResolver, name: str
) -> None:
    """The sound name comes from Home Assistant, so it is untrusted input."""
    with pytest.raises(MediaError):
        await resolver.resolve(f"sound:{name}")


async def test_list_sounds(resolver: MediaResolver, sounds_dir: Path, make_wav) -> None:
    make_wav(sounds_dir / "chime.wav")
    make_wav(sounds_dir / "evacuate.wav")
    assert resolver.list_sounds() == ["chime", "evacuate"]


async def test_concat_joins_chime_and_announcement(
    resolver: MediaResolver, sounds_dir: Path, make_wav
) -> None:
    """One file rather than two players: a gap between players is audible on a
    page and risks the tail being cut."""
    make_wav(sounds_dir / "chime.wav", seconds=1.0)
    make_wav(sounds_dir / "message.wav", seconds=2.0)

    joined = await resolver.concat(
        [await resolver.resolve("sound:chime"), await resolver.resolve("sound:message")]
    )

    assert joined.duration == pytest.approx(3.0, abs=0.1)
    assert _probe(joined.path)["rate"] == 8000


async def test_concat_of_one_clip_is_that_clip(
    resolver: MediaResolver, sounds_dir: Path, make_wav
) -> None:
    make_wav(sounds_dir / "chime.wav")
    clip = await resolver.resolve("sound:chime")
    assert (await resolver.concat([clip])).path == clip.path


async def test_concat_of_nothing_is_an_error(resolver: MediaResolver) -> None:
    with pytest.raises(MediaError, match="nothing to play"):
        await resolver.concat([])


async def test_url_is_cached_by_content_not_url(
    resolver: MediaResolver, sounds_dir: Path, make_wav, monkeypatch
) -> None:
    """Home Assistant TTS URLs carry a per-request token, so the same phrase
    arrives under a new URL every time. A URL-keyed cache would never hit."""
    source = make_wav(sounds_dir / "_src.wav", seconds=1.0)
    payload = source.read_bytes()
    fetches: list[str] = []

    async def fake_fetch(url: str, headers: dict) -> bytes:
        fetches.append(url)
        return payload

    monkeypatch.setattr(resolver, "_fetch", fake_fetch)

    first = await resolver.resolve("http://ha.test/api/tts_proxy/TOKEN-A.mp3")
    second = await resolver.resolve("http://ha.test/api/tts_proxy/TOKEN-B.mp3")

    assert len(fetches) == 2          # both were fetched...
    assert second.cached              # ...but identical content skipped the transcode
    assert second.path == first.path


async def test_cache_eviction_respects_the_budget(
    cache_dir: Path, sounds_dir: Path, make_wav, monkeypatch
) -> None:
    resolver = MediaResolver(
        cache_dir=cache_dir, sounds_dir=sounds_dir, sample_rate=8000, cache_max_bytes=20_000
    )
    source = make_wav(sounds_dir / "_src.wav", seconds=1.0)

    async def fetch_n(n: int) -> None:
        payload = make_wav(sounds_dir / f"_s{n}.wav", seconds=1.0, freq=200 + n * 50).read_bytes()

        async def fake(url: str, headers: dict) -> bytes:
            return payload

        monkeypatch.setattr(resolver, "_fetch", fake)
        await resolver.resolve(f"http://ha.test/{n}.wav")

    for n in range(6):
        await fetch_n(n)

    total = sum(p.stat().st_size for p in cache_dir.glob("*.wav"))
    assert total <= 20_000


async def test_unfetchable_url_is_a_media_error(resolver: MediaResolver) -> None:
    with pytest.raises(MediaError):
        await resolver.resolve("http://127.0.0.1:1/nothing.mp3")


async def test_missing_local_file(resolver: MediaResolver) -> None:
    with pytest.raises(MediaError, match="no such file"):
        await resolver.resolve("/nonexistent/clip.wav")


async def test_ffmpeg_missing_fails_loudly(monkeypatch) -> None:
    """A broken image must announce itself at startup, not at page time."""
    monkeypatch.setattr("app.media.shutil.which", lambda name: None)
    with pytest.raises(MediaError, match="not found on PATH"):
        await MediaResolver.assert_ffmpeg()


async def test_url_label_hides_the_token(
    resolver: MediaResolver, sounds_dir: Path, make_wav, monkeypatch
) -> None:
    """A Home Assistant TTS URL carries a token that grants access to the audio."""
    payload = make_wav(sounds_dir / "_src.wav").read_bytes()

    async def fake(url: str, headers: dict) -> bytes:
        return payload

    monkeypatch.setattr(resolver, "_fetch", fake)
    clip = await resolver.resolve("http://ha.test:8123/api/tts_proxy/SECRET-TOKEN.mp3")

    assert "SECRET-TOKEN" not in clip.label
    assert "ha.test:8123" in clip.label       # host is still useful for debugging


async def test_sound_label_is_its_name(
    resolver: MediaResolver, sounds_dir: Path, make_wav
) -> None:
    make_wav(sounds_dir / "chime.wav")
    assert (await resolver.resolve("sound:chime")).label == "sound:chime"


async def test_builtin_sounds_are_found(cache_dir: Path, tmp_path: Path, make_wav) -> None:
    """An add-on mounts /data as its own volume, which would shadow anything the
    image baked in there. Built-ins therefore live outside it."""
    user_dir = tmp_path / "user"
    builtin = tmp_path / "builtin"
    user_dir.mkdir()
    builtin.mkdir()
    make_wav(builtin / "chime.wav")

    resolver = MediaResolver(
        cache_dir=cache_dir, sounds_dir=user_dir, sample_rate=8000, builtin_dir=builtin
    )
    assert resolver.list_sounds() == ["chime"]
    assert (await resolver.resolve("sound:chime")).duration > 0


async def test_user_sound_overrides_a_builtin(
    cache_dir: Path, tmp_path: Path, make_wav
) -> None:
    user_dir = tmp_path / "user"
    builtin = tmp_path / "builtin"
    user_dir.mkdir()
    builtin.mkdir()
    make_wav(builtin / "chime.wav", seconds=1.0)
    make_wav(user_dir / "chime.wav", seconds=2.0)

    resolver = MediaResolver(
        cache_dir=cache_dir, sounds_dir=user_dir, sample_rate=8000, builtin_dir=builtin
    )
    clip = await resolver.resolve("sound:chime")
    assert clip.duration == pytest.approx(2.0, abs=0.1)   # the user's, not the builtin
    assert resolver.list_sounds() == ["chime"]            # listed once, not twice
