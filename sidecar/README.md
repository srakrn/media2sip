# Media2SIP sidecar

A SIP user agent that pages PBX extensions on behalf of Home Assistant, plus a
local control API. This is the only component with a SIP stack; the Home
Assistant integration has no non-standard dependencies.

> **This is a developer reference** — the control API, the media pipeline and the
> threading rules. If you just want to run it, start at
> [`docs/user/installation.md`](../docs/user/installation.md); every setting is in
> [`docs/user/configuration.md`](../docs/user/configuration.md), and the split
> between the two halves is in
> [`docs/dev/architecture.md`](../docs/dev/architecture.md).

Runs as a **plain Docker container** or as a **Home Assistant add-on** from the
same image — `app/config.py` reads the supervisor's `/data/options.json` when
there is one and environment variables otherwise, so nothing above it has to know
which shape it is running in.

## Quick start

```sh
cp ../.env.example ../.env      # then fill in SIP_* / PBX_*
docker compose up -d --build
curl -s localhost:8080/health | jq
```

Page an extension:

```sh
curl -X POST localhost:8080/call -H 'content-type: application/json' -d '{
  "target": "991",
  "chime":  "sound:chime",
  "media":  "http://homeassistant.local:8123/api/tts_proxy/....mp3"
}'
```

## Control API

| | |
| --- | --- |
| `GET /health` | Registration state per account, active calls, available sounds, pinned codecs. `status` is `degraded` when any account is unregistered. |
| `POST /call` | Place a page. Returns a `call_id`. Media is resolved *before* the call is placed, so a bad clip is a `400` rather than handsets answering to silence. |
| `DELETE /call/{id}` | Hang up. |
| `POST /call/{id}/pause` | `{"paused": true|false}`. Holds the clip without dropping the call. |
| `GET /calls` | Calls in flight. |
| `GET /calls/history?limit=` | Recent finished calls: target, SIP reason code, latency split into the PBX's part and ours, and whether any RTP actually went down the wire. |
| `GET /sounds` | Static sounds, from your volume and the ones built into the image. |
| `GET /events/recent?limit=` | Recent events, so a reconnecting client can see what it missed. |
| `WS /ws` | Event stream. Sends a `hello` frame with registration state on connect. |

`POST /call` body: `target`, `media` (an `http(s)` URL, `sound:<name>`, or a path
on the sounds volume), `chime`, `lead_in`, `account_id`, `policy`, `headers`.

Events, in the order a healthy page goes through them: `calling`, `early`,
`confirmed`, `playback_started`, `playback_finished`, `disconnected` — plus
`registered` / `unregistered`. Every event carries `call_id`, `state`, `elapsed`,
`sip_code` and `sip_reason`, because a silent failure is the main way an
announcement system loses trust.

Set `API_TOKEN` to require `Authorization: Bearer`. It is unset by default and the
sidecar warns loudly at startup when it is; that is only acceptable on a private
network.

## Concurrency

`policy` is `reject` (default, returns `409`), `replace`, or `preempt`.

`replace` swaps the audio on the call that is already up: it starts on the next
frame, needs no lead-in because the handsets are already listening, and keeps the
same `call_id`. `preempt` hangs up and re-originates, which drops the page group
and makes it answer again — use it only when you want a genuinely fresh call.

**Queueing is deliberately not here** — it needs entity-level semantics and
belongs in the integration.

## How a page actually works

1. Resolve the media. `sound:` clips come off the volume; URLs are fetched and
   cached **by content hash**, not by URL, because Home Assistant's TTS URLs carry
   a per-request token and a URL-keyed cache would never hit.
2. Transcode to 16-bit PCM mono 8 kHz with ffmpeg. The page group negotiates
   PCMU 8 kHz and offers no G.722, so the codec list is *pinned*
   rather than left to pjsua2's defaults — offering codecs the PBX will never pick
   only widens the surface for a one-way-audio bug.
3. Place the call, wait for `CONFIRMED`, then **wait the lead-in** before playing.
   Auto-answering handsets need about a second to open the audio path; without it
   the first word is clipped. This is not optional.
4. Hang up when `AudioMediaPlayer.onEof2` fires, with a duration-derived timer as
   the backstop, then file a history record with the RTP packet counters — a page
   that sends no RTP looks identical to a working one from the SIP dialog alone.

## Threading

pjsua2 is a C++ library with its own threading and mixing it with asyncio is where
this kind of code usually goes wrong. The rule here is strict: **every pjsua2 call
and every pjsua2 callback happens on one thread.** The endpoint is created with
`threadCnt = 0` so pjsua2 spawns no workers; a single `SipWorker` thread pumps
`libHandleEvents()` and drains a command queue between pumps. That means no
`libRegisterThread`, no locks around the stack, and no callback arriving on a
thread that is midway through an API call. Results return to asyncio through
futures resolved with `call_soon_threadsafe`.

## Layout

### Sounds

Sounds ship in the image at `/opt/media2sip/sounds` and your own go in
`/data/sounds`, searched first so you can override one by name. They are separate
because a Home Assistant add-on gets `/data` mounted as its persistent volume,
which would otherwise shadow everything the image baked in there.

```
Dockerfile                  pjproject 2.15.1 + SWIG bindings, multi-stage
docker-compose.yml          host networking (the right answer on Linux)
docker-compose.bridge.yml   overlay for hosts without usable host networking
app/config.py               env vars and/or add-on options.json
app/sip.py                  pjsua2 endpoint, call lifecycle, playback
app/media.py                ffmpeg transcode, content-hash cache, ffprobe duration
app/events.py               thread-safe event bus
app/main.py                 FastAPI control API + websocket
sounds/                     clips baked into the image
tests/                      unit tests; tests/integration is the Asterisk suite
```

Building pjproject takes a few minutes; the compile is its own Docker layer, so
edits to `app/` rebuild in seconds.
