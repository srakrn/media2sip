"""End-to-end: the sidecar against a real Asterisk.

Run with `./tests/integration/run.sh`, which brings the stack up first. These are
the tests that cannot be faked - registration, SDP negotiation, RTP carrying
actual audio, and the SIP failure codes - and none of them need FreePBX, which is
what makes them practical to run in CI.

Asterisk records what it hears, so "did the page arrive" is answered by measuring
the far end's audio rather than by trusting that a call connected.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import time
import urllib.error
import urllib.request
import wave

import pytest

SIDECAR = "http://127.0.0.1:18080"
INSPECTOR = "pbx-page-e2e-inspector"
RECORDING = "/recordings/page.wav"


# -- helpers -------------------------------------------------------------


def api(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{SIDECAR}{path}", data=data, method=method,
        headers={"content-type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def inspector(*args: str) -> str:
    return subprocess.run(
        ["docker", "exec", INSPECTOR, *args], capture_output=True, text=True
    ).stdout


def reset_recording() -> None:
    subprocess.run(["docker", "exec", INSPECTOR, "rm", "-f", RECORDING], check=False)


def asterisk(command: str) -> str:
    return subprocess.run(
        ["docker", "exec", "pbx-page-e2e-asterisk", "asterisk", "-rx", command],
        capture_output=True, text=True,
    ).stdout


def wait_idle(timeout: float = 30.0) -> None:
    """Wait for the call to be over on **both** sides.

    Waiting only for the sidecar is not enough. It reports idle the moment it
    hangs up, while Asterisk is still running the dialplan and closing the
    recording. Every test writes to the same file, so a slow runner can let one
    test's recording land on top of the next test's - which is exactly how this
    suite failed in CI while passing locally: a lead-in test measured the
    previous test's audio and read its lead-in instead of its own.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, health = api("GET", "/health")
        if not health.get("active_calls") and "0 active channels" in asterisk("core show channels count"):
            return
        time.sleep(0.25)
    raise AssertionError(
        f"still busy after {timeout}s: sidecar={api('GET', '/health')[1].get('active_calls')} "
        f"asterisk={asterisk('core show channels count').strip()}"
    )


def measure(raw: bytes) -> dict:
    """Duration, level, and where sound starts and stops, for any 16-bit WAV."""
    with open("/tmp/e2e-measure.wav", "wb") as handle:
        handle.write(raw)
    with wave.open("/tmp/e2e-measure.wav") as w:
        rate = w.getframerate()
        frames = w.getnframes()
        samples = struct.unpack(f"<{frames * w.getnchannels()}h", w.readframes(frames))

    window = rate // 20
    loud = [
        i
        for i in range(0, len(samples) - window, window)
        if max(abs(s) for s in samples[i : i + window]) > 500
    ]
    onset = loud[0] / rate if loud else None
    end = (loud[-1] + window) / rate if loud else None
    return {
        "rate": rate,
        "duration": frames / rate,
        "peak": max(abs(s) for s in samples),
        "rms": math.sqrt(sum(s * s for s in samples) / len(samples)),
        "onset": onset,
        # First to last audible sample: how much of the clip actually arrived.
        "span": None if onset is None else end - onset,
    }


def source_clip(name: str) -> dict:
    """The clip as the sidecar holds it, measured the same way as the recording.

    Comparing the two is a round trip. Comparing the recording against the clip's
    *duration* would not work: clips carry trailing silence, so an intact page
    still sounds shorter than the file is long.
    """
    raw = subprocess.run(
        ["docker", "exec", INSPECTOR, "cat", f"/opt/pbx-page/sounds/{name}.wav"],
        capture_output=True,
    ).stdout
    assert raw, f"the sidecar image has no {name}.wav"
    return measure(raw)


def analyse() -> dict:
    """Measure the far end's recording: duration, level, and where sound starts."""
    # Read only once the file has stopped growing. Asterisk writes it
    # progressively, and a half-written read would be measured as a short clip.
    raw = b""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = subprocess.run(
            ["docker", "exec", INSPECTOR, "cat", RECORDING], capture_output=True
        ).stdout
        if current and current == raw:
            break
        raw = current
        time.sleep(0.25)
    assert raw, "Asterisk recorded nothing"
    return measure(raw)


def history_for(call_id: str) -> dict:
    _, history = api("GET", "/calls/history?limit=10")
    record = next((c for c in history if c["call_id"] == call_id), None)
    assert record is not None, f"no history for {call_id}"
    return record


@pytest.fixture(autouse=True)
def _settle():
    wait_idle()
    yield
    wait_idle()


# -- tests ---------------------------------------------------------------


def test_sidecar_registers_with_asterisk() -> None:
    status, health = api("GET", "/health")
    assert status == 200
    assert health["status"] == "ok"
    assert health["accounts"][0]["registered"] is True


def test_asterisk_sees_the_contact() -> None:
    out = subprocess.run(
        ["docker", "exec", "pbx-page-e2e-asterisk", "asterisk", "-rx", "pjsip show aors"],
        capture_output=True, text=True,
    ).stdout
    assert "9901" in out


def test_a_page_delivers_real_audio() -> None:
    """The whole point of the project, measured at the far end."""
    reset_recording()
    status, body = api("POST", "/call", {"target": "991", "chime": "sound:chime"})
    assert status == 200, body
    wait_idle()

    audio = analyse()
    assert audio["rate"] == 8000                      # narrowband, as negotiated
    assert audio["peak"] > 500, "the page was silent"
    assert audio["rms"] > 100


# Why the lead-in is not measured from the recording's timeline
# -------------------------------------------------------------
# The obvious test - "page with lead_in 1.0, check the recording's first sound is
# at 1.0s" - passes in isolation and fails in a suite. Asterisk starts writing
# the file some way after it answers, and on a back-to-back call that delay was
# measured at ~0.6s, which subtracts straight off the apparent onset. The
# recording's zero is the recorder's, not the call's.
#
# So the lead-in is measured where it is actually defined - between the answer
# and playback starting, which the sidecar reports - and the recording is used
# for the thing it can prove: that the audio arrived whole.

@pytest.mark.parametrize("lead_in", [0.0, 0.5, 1.5])
def test_lead_in_is_honoured(lead_in: float) -> None:
    """Auto-answering handsets need a moment to open the audio path; without the
    lead-in the first word is clipped."""
    _, body = api(
        "POST", "/call", {"target": "991", "chime": "sound:chime", "lead_in": lead_in}
    )
    wait_idle()

    record = history_for(body["call_id"])
    waited = record["playback_latency"] - record["answer_latency"]
    assert waited == pytest.approx(lead_in, abs=0.15), record


def test_audio_arrives_whole_and_unclipped() -> None:
    """The property the lead-in exists to protect.

    With a lead-in longer than the far end takes to start recording, every last
    sample of the clip is captured. A clipped first word shows up here as an
    audible span shorter than the clip.
    """
    reset_recording()
    api("POST", "/call", {"target": "991", "chime": "sound:chime", "lead_in": 1.5})
    wait_idle()

    audio = analyse()
    source = source_clip("chime")
    assert audio["onset"] is not None, "no audio in the recording at all"
    assert audio["span"] == pytest.approx(source["span"], abs=0.15), (
        f"heard {audio['span']:.2f}s of a {source['span']:.2f}s clip"
    )


def test_busy_target_returns_486() -> None:
    """A busy page group returns a real 486, so `reject` is cheap and correct.

    POST /call succeeds: the call is placed, and the far end rejects it a moment
    later. The failure therefore arrives as a disconnect event carrying the SIP
    code, which is exactly the signal the integration surfaces.
    """
    status, body = api("POST", "/call", {"target": "992", "chime": "sound:chime"})
    assert status == 200, body
    wait_idle()

    _, events = api("GET", "/events/recent?limit=10")
    disconnects = [e for e in events if e["type"] == "disconnected"]
    assert disconnects, events
    assert disconnects[-1]["sip_code"] == 486, disconnects[-1]


def test_unanswered_call_times_out_and_releases() -> None:
    """A call nobody answers must not wedge the target forever."""
    status, body = api("POST", "/call", {"target": "993", "chime": "sound:chime"})
    assert status == 200, body
    wait_idle(timeout=30)

    _, events = api("GET", "/events/recent?limit=20")
    reasons = [e.get("reason") for e in events if e["type"] == "disconnected"]
    assert "answer_timeout" in reasons, reasons


def test_hangup_stops_a_page_in_flight() -> None:
    status, body = api("POST", "/call", {"target": "991", "chime": "sound:chime"})
    assert status == 200
    assert api("DELETE", f"/call/{body['call_id']}")[0] == 200
    wait_idle()
    assert not api("GET", "/health")[1]["active_calls"]


def test_second_page_to_a_busy_target_is_rejected() -> None:
    status, first = api("POST", "/call", {"target": "991", "media": "sound:chime"})
    assert status == 200
    status, _ = api("POST", "/call", {"target": "991", "media": "sound:chime"})
    assert status == 409
    api("DELETE", f"/call/{first['call_id']}")
    wait_idle()


def test_history_records_a_successful_page() -> None:
    """Latency split into the PBX's part and ours, plus proof audio was sent."""
    reset_recording()
    _, body = api("POST", "/call", {"target": "991", "chime": "sound:chime"})
    wait_idle()

    _, history = api("GET", "/calls/history?limit=5")
    record = next(c for c in history if c["call_id"] == body["call_id"])

    assert record["target"] == "991"
    assert record["sip_code"] == 200
    assert record["end_reason"] == "playback_complete"
    assert record["audio_sent"] is True
    assert record["rtp"]["tx_pkt"] > 0
    assert 0 <= record["answer_latency"] < 2
    # Playback starts a lead-in after the answer, never before it.
    assert record["playback_latency"] > record["answer_latency"]


def test_history_explains_a_failed_page() -> None:
    """The whole point: a failure has to say why, and say that nothing was heard."""
    _, body = api("POST", "/call", {"target": "992", "chime": "sound:chime"})
    wait_idle()

    _, history = api("GET", "/calls/history?limit=5")
    record = next(c for c in history if c["call_id"] == body["call_id"])

    assert record["sip_code"] == 486
    assert record["audio_sent"] is False


def test_history_distinguishes_an_answer_timeout() -> None:
    """An answer timeout must not be filed as an ordinary disconnect."""
    _, body = api("POST", "/call", {"target": "993", "chime": "sound:chime"})
    wait_idle(timeout=30)

    _, history = api("GET", "/calls/history?limit=5")
    record = next(c for c in history if c["call_id"] == body["call_id"])

    assert record["end_reason"] == "answer_timeout"
    assert record["answer_latency"] is None


def test_history_media_label_is_not_a_url() -> None:
    _, body = api("POST", "/call", {"target": "991", "media": "sound:chime"})
    wait_idle()
    _, history = api("GET", "/calls/history?limit=5")
    record = next(c for c in history if c["call_id"] == body["call_id"])
    assert record["media"] == "sound:chime"


# -- pause and swap ------------------------------------------------------


@pytest.fixture
def long_clip():
    """A clip with room to pause in the middle. Built here rather than shipped,
    so the image is not carrying a test fixture."""
    subprocess.run(
        ["docker", "exec", "pbx-page-e2e-sidecar", "sh", "-c",
         "ffmpeg -y -v error -f lavfi -i 'sine=frequency=660:duration=6' "
         "-ac 1 -ar 8000 -acodec pcm_s16le /data/sounds/long.wav"],
        check=True,
    )
    return "sound:long"


def test_pause_holds_the_call_and_resumes_where_it_stopped(long_clip) -> None:
    """Pause disconnects the player from the call rather than stopping RTP.

    Two things have to be true, and neither is obvious: the call must survive the
    pause (the far end must not see a media timeout), and the clip must not run
    on while nobody is listening to it. The second is measured at the far end -
    the recording carries the whole clip plus a silent gap, not a clip with a
    hole punched in it.
    """
    reset_recording()
    _, body = api("POST", "/call", {"target": "991", "media": long_clip, "lead_in": 0.5})
    call_id = body["call_id"]

    time.sleep(2.0)
    assert api("POST", f"/call/{call_id}/pause", {"paused": True})[0] == 200

    time.sleep(2.0)
    _, health = api("GET", "/health")
    live = [c for c in health["active_calls"] if c["call_id"] == call_id]
    assert live, "the call was dropped while paused"
    assert live[0]["paused"] is True

    assert api("POST", f"/call/{call_id}/pause", {"paused": False})[0] == 200
    wait_idle(timeout=40)

    audio = analyse()
    source = measure(
        subprocess.run(
            ["docker", "exec", "pbx-page-e2e-sidecar", "cat", "/data/sounds/long.wav"],
            capture_output=True,
        ).stdout
    )
    # Everything arrived, plus the silence we asked for. Had the clip run on
    # while paused, the span would be shorter than the source, not longer.
    assert audio["span"] > source["span"] + 1.0
    assert audio["span"] < source["span"] + 4.0


def test_pausing_a_call_that_plays_nothing_is_refused() -> None:
    status, _ = api("POST", "/call/nope/pause", {"paused": True})
    assert status >= 400


def test_replace_swaps_the_clip_without_re_dialling(long_clip) -> None:
    """One call, two clips. The handsets never answer twice."""
    wait_idle()
    _, first = api("POST", "/call", {"target": "991", "media": long_clip, "lead_in": 0.5})
    time.sleep(1.5)

    status, second = api(
        "POST", "/call", {"target": "991", "media": "sound:chime", "policy": "replace"}
    )
    assert status == 200
    assert second["replaced"] is True
    assert second["call_id"] == first["call_id"]

    wait_idle(timeout=30)
    _, history = api("GET", "/calls/history?limit=10")
    entries = [c for c in history if c["call_id"] == first["call_id"]]
    assert len(entries) == 1, "a replace must not produce a second call"
    assert entries[0]["media"] == "sound:chime"
    assert entries[0]["audio_sent"] is True
