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


def wait_idle(timeout: float = 30.0) -> None:
    """Wait for no call to be in flight, so tests do not tread on each other."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, health = api("GET", "/health")
        if not health.get("active_calls"):
            return
        time.sleep(0.25)
    raise AssertionError("a call was still in flight")


def analyse() -> dict:
    """Measure the far end's recording: duration, level, and where sound starts."""
    raw = subprocess.run(
        ["docker", "exec", INSPECTOR, "cat", RECORDING], capture_output=True
    ).stdout
    assert raw, "Asterisk recorded nothing"

    with open("/tmp/e2e-page.wav", "wb") as handle:
        handle.write(raw)
    with wave.open("/tmp/e2e-page.wav") as w:
        rate = w.getframerate()
        frames = w.getnframes()
        samples = struct.unpack(f"<{frames * w.getnchannels()}h", w.readframes(frames))

    window = rate // 20
    onset = next(
        (
            i / rate
            for i in range(0, len(samples) - window, window)
            if max(abs(s) for s in samples[i : i + window]) > 500
        ),
        None,
    )
    return {
        "rate": rate,
        "duration": frames / rate,
        "peak": max(abs(s) for s in samples),
        "rms": math.sqrt(sum(s * s for s in samples) / len(samples)),
        "onset": onset,
    }


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


def test_lead_in_is_honoured_at_the_far_end() -> None:
    """Auto-answering handsets need a moment to open the audio path; without the
    lead-in the first word is clipped. This measures that it is really there."""
    reset_recording()
    api("POST", "/call", {"target": "991", "chime": "sound:chime", "lead_in": 1.0})
    wait_idle()

    audio = analyse()
    assert audio["onset"] is not None, "no audio in the recording at all"
    assert audio["onset"] == pytest.approx(1.0, abs=0.25)


def test_shorter_lead_in_starts_sooner() -> None:
    """Proves the onset tracks the setting rather than being a fixed artefact."""
    reset_recording()
    api("POST", "/call", {"target": "991", "chime": "sound:chime", "lead_in": 0.0})
    wait_idle()
    assert analyse()["onset"] < 0.4


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
