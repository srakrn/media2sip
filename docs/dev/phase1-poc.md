# Phase 1: manual proof of concept — done

**Acceptance, from the master plan:** *audible speech from the handsets, originated by a registered
extension, with no changes made on the PBX beyond creating that extension.*

**Met**, 2026-08-31. Confirmed audible by the user on the second run.

## What was done

1. Validated the credentials for extension 9901 with a hand-rolled digest `REGISTER` (a throwaway
   script, not kept) — `200 OK`, one binding.
2. Registered `baresip` 4.11.0 as 9901 against `10.1.2.99`.
3. Dialled **991** and streamed a WAV into the confirmed call via baresip's `aufile` audio source,
   which is the exact mechanism the sidecar will use.
4. Hung up on a timer derived from `ffprobe` clip duration.

Rig lives in [phase1/](../../phase1/) and is re-runnable: `./phase1/page.sh 991 chime_announce`.

## What the runs proved

- **The softphone-emulation architecture works as specified.** One GUI-created extension, no
  dialplan, no AMI, no manager user.
- **Codec:** PCMU 8000 Hz mono, ptime 20. Pin the sidecar's offer to this.
- **No `180 Ringing`:** the page group answers immediately. Call setup to `Call established` was
  sub-second on the LAN, so the master plan's three-second budget is comfortable.
- **Two-way RTP** established with no NAT traversal machinery.

## What the runs changed

### The lead-in is real, and the clip is the wrong place to put it

The first run used a clip that opens on speech with no leading silence. The second prefixed a 0.9 s
chime. Padding inside the media file works, but it means every dynamic TTS clip needs re-encoding
just to prepend silence.

The sidecar should keep the master plan's design instead: reach `CONFIRMED`, **wait** the configured
lead-in, *then* start playback. Default 1 second, as planned.

### baresip's `aufile` does not signal end-of-playback

At clip EOF the audio source does not report completion; it underruns and floods the log with
`tx aubuf underrun`, and the call stays up until something else tears it down. Playback end has to
be driven from a known duration (`ffprobe`) plus the lead-in.

That is what the master plan already specified for the timeout, so nothing changes there — but it
does weaken `baresip` as plan B. `pjsua2`'s `AudioMediaPlayer` emits an end-of-file callback, which
is a genuinely better fit than inferring completion from a stopwatch. **Recommend staying with
`pjsua2` for the sidecar**; keep baresip as the fallback it was always meant to be, with the caveat
recorded.

### The homebrew baresip build ships no `g722.so`

Irrelevant now that PCMU is what gets negotiated, but relevant if baresip is ever picked up as
plan B against a different PBX.

## Still open after phase 1

- Home Assistant install type and container network mode (blocks phase 2 packaging).
- Whether an MQTT broker is available (decides the control transport).
- Whether Groundwire on iPhone auto-answers rather than ringing.
