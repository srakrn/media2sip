# Plan: expose SIP paging extensions as Home Assistant media players

> **Status, 2026-08-31.** Phase 0 skipped as a discrete step; the parts of it that mattered were
> answered in passing by phase 1 and are recorded in [`docs/environment.md`](../docs/environment.md).
> **Phase 1 is done and accepted** — see [`docs/phase1-poc.md`](../docs/phase1-poc.md). Paging target
> is **991** (not 211), the UA is extension **9901**, and the negotiated codec is **PCMU 8 kHz mono**.
> **Phase 2 (the sidecar) is built and working** — it pages 991 with live Home Assistant TTS from a
> Docker container; see [`sidecar/`](../sidecar/) and [`docs/deployment.md`](../docs/deployment.md).
> Home Assistant here is a **Container** install, so plain Docker is the target and the add-on
> manifest is portability. **Phases 3, 4 and 5 are built** — see
> [`docs/integration.md`](../docs/integration.md). The definition of done is met: `tts.speak`
> against `media_player.working_zone` pages the handsets. **Phase 6 is complete** — 112 tests across
> three suites ([`docs/testing.md`](../docs/testing.md)), per-call diagnostics, and packaging for
> both HACS and the add-on store. **All phases are done.**

## Confirmed scope

- PBX is **FreePBX** (migrated from Issabel), but the design must not depend on that.
- Payload is **short TTS clips, pre-recorded announcements, and chimes only**. Seconds, not minutes.

## Goal

A custom integration creating one `media_player` entity per paging target (one target is one SIP extension). `tts.speak` or
`media_player.play_media` against that entity causes a SIP user agent to place a call to the
paging extension, the handsets auto-answer, the clip plays, the call hangs up.

## Architecture decision: softphone emulation, not AMI

A SIP user agent registers as an ordinary extension and dials the page group. **Zero PBX-side
configuration beyond creating one extension in the GUI.** No manager user, no dialplan edits, no
files that FreePBX regenerates or that a restore silently drops.

Consequences worth stating plainly:

- **Portable.** Anything with a SIP registrar works: Asterisk, FreeSWITCH, Kamailio, a hosted PBX.
  Migrating means changing credentials, nothing else.
- **Better state.** Call state comes from the SIP dialog directly (calling, early, confirmed,
  disconnected) rather than being inferred from AMI events. Busy is a 486, not a missing event.
  This removes the correlation-token scheme, the `UserEvent` dialplan, and most watchdog logic.
- **Media stays local.** No HTTP fetch from the PBX, no `res_http_media_cache` dependency, no
  unauthenticated media endpoint, no guessing which decoders the PBX has.

### Why this needs a sidecar

No usable SIP stack runs inside the Home Assistant Python process. `pjsua2` has no reliable
manylinux wheel, and RTP wants a UDP port range that the integration cannot claim. This is why
`arnonym/ha-plugins` ships as a container rather than an integration.

So: **a SIP UA sidecar plus a thin Home Assistant integration** that owns the entity and speaks to
the sidecar over a local control API. The sidecar is the only component with a SIP stack; the
integration has no non-standard dependencies.

### Sidecar stack

- **`pjsua2` in a container (recommended).** `AudioMediaPlayer` streaming a WAV into a confirmed
  call is the well-trodden path, and ha-sip is the existence proof that it works against FreePBX.
- `baresip` with the `aufile` module and its netstring control interface is a lighter alternative
  if the pjproject build proves painful. Keep it as plan B — but note phase 1 found `aufile` gives
  no end-of-playback signal, so a baresip sidecar can only ever stopwatch its way through playback.
- **Shortcut for v0:** run ha-sip in standalone MQTT mode and point the integration at it. This
  de-risks everything before writing a line of SIP code, at the cost of its command surface being
  fixed. Treat it as a spike, not the destination.

Packaging: a Home Assistant add-on if the install is HAOS or supervised, a plain Docker container
otherwise. The integration must support both, so the control API cannot assume `hassio.addon_stdin`.

### Control API between integration and sidecar

Local HTTP plus a websocket for events, or MQTT if a broker is already running. Minimum surface:

- `POST /call` with target extension, media reference, lead-in, optional chime. Returns a call id.
- `DELETE /call/{id}` to hang up.
- `GET /health` with registration state per account.
- Websocket or MQTT event stream: `registered`, `unregistered`, `calling`, `early`, `confirmed`,
  `playback_started`, `playback_finished`, `disconnected` with a SIP reason code.

Design the API so it is stack-agnostic. If `pjsua2` is later swapped for `baresip`, only the
sidecar changes.

## Phase 0: recon — skipped, mostly answered by phase 1

Running the phase 1 PoC turned out to answer most of this for free, and answer it better: measured
from a real call rather than read off a config screen. Findings are in
[`docs/environment.md`](../docs/environment.md). Settled:

- Paging target is **991**, not the 211 carried over from Issabel. It answers `200 OK` immediately
  with no `180 Ringing`, so the page group auto-answers.
- Negotiated codec is **PCMU 8 kHz mono, ptime 20**. No G.722, so **serve narrowband** — the
  wideband-TTS branch is closed.
- PBX is FreePBX 17.0.19.32 / Asterisk 22.10.1, `chan_pjsip` on UDP 5060.

Still genuinely open, and carried forward:

1. ~~Home Assistant install type and network mode.~~ **Container** install at `10.1.2.96`, so plain
   Docker. Bridged containers work against this PBX; host networking is the Linux default.
2. ~~Whether an MQTT broker is available.~~ One exists, but the control transport is HTTP plus a
   websocket regardless, so the sidecar carries no external dependency.
3. **Whether Groundwire on iPhone auto-answers** rather than ringing. The group auto-answers; that
   particular member was not exercised. Still open.

Note what is **not** on this list any more: Asterisk version, module availability, manager users,
dialplan file paths. That is the point of the change.

## Phase 1: manual proof of concept, no code — **done**

`baresip` registered as extension 9901, dialled 991, and streamed a WAV into the confirmed call via
its `aufile` audio source. Audible from the handsets, confirmed by ear. The only PBX-side change was
creating extension 9901 in the GUI, exactly as the architecture promised.

Re-runnable rig in [`phase1/`](../phase1/): `./phase1/page.sh 991 chime_announce`.

Acceptance met. Full write-up, including what it changed downstream, in
[`docs/phase1-poc.md`](../docs/phase1-poc.md). The two findings that touch later phases:

- **`baresip`'s `aufile` never signals end-of-playback** — it underruns silently at EOF. Completion
  has to be inferred from `ffprobe` duration. That is already the plan for the timeout, but it makes
  `pjsua2` (whose `AudioMediaPlayer` emits a real end-of-file callback) the clearly better primary
  choice rather than merely the recommended one. Baresip stays plan B with this caveat attached.
- **Padding the lead-in into the media file works but is the wrong layer** — it would force a
  re-encode of every dynamic TTS clip. Keep the lead-in in the sidecar as a wait between `CONFIRMED`
  and playback start, as already specified.

## Phase 2: the sidecar — **built**

Built as specified, with `app/config.py` and `app/events.py` added to the sketch. It registers as
9901, pages 991 with live `tts.openai_tts` audio, and reports every state transition. See
[`sidecar/README.md`](../sidecar/README.md).

```
sidecar/
  Dockerfile             # pjproject 2.15.1 + SWIG bindings, multi-stage
  app/main.py            # control API
  app/sip.py             # pjsua2 account, call lifecycle, playback
  app/media.py           # ffmpeg transcode, on-disk cache keyed by content hash
  app/config.py          # env vars and/or add-on options.json - one image, both shapes
  app/events.py          # thread-safe event bus
  addon/config.yaml      # Home Assistant add-on manifest
```

What the build added to the design:

- **Threading rule.** The endpoint runs with `threadCnt = 0` and a single thread pumps
  `libHandleEvents()` and a command queue, so every pjsua2 call *and* every callback is on one
  thread. No `libRegisterThread`, no locks around the stack.
- **`onEof2` works.** `AudioMediaPlayer` is a SWIG director class, so playback completion is a real
  callback and the duration timer is only a backstop — the improvement over baresip that phase 1
  predicted.
- **RTP counters are logged at teardown.** A call that sends no RTP is indistinguishable from a
  working one by its SIP dialog alone, and that is exactly the silent failure this project cannot
  afford.
- **Codecs are pinned, not merely preferred.** Everything outside the configured list gets priority
  zero, so the offer is exactly PCMU then PCMA.

Two build snags worth recording: pjproject's SWIG `setup.py` still imports `distutils` (gone in
Python 3.12, so setuptools is installed for its shim), and its `make install` target calls the
long-removed `setup.py install --user`, so the built artifacts are placed by hand.

Behaviour:

- Register one account per configured PBX. Re-REGISTER handling and reconnect with backoff.
- On `POST /call`: resolve media, place the call, wait for `CONFIRMED`, wait the configured
  lead-in, then start playback. **The lead-in is not optional.** Auto-answering handsets need
  about a second to open the audio path, and without it the first word is clipped.
- Hang up on playback completion, on explicit request, or on timeout.
- Emit every state transition, including the SIP reason code on failure. Silent failures are the
  main way an announcement system loses trust.

Media handling:

- **Static sounds** (chimes, fixed announcements): converted once by `scripts/sync-sounds.sh` and
  held on the sidecar's volume. No fetch, no transcode at call time.
- **Dynamic TTS:** the integration passes a resolved URL; the sidecar fetches, transcodes with
  ffmpeg to 16-bit PCM WAV mono at the negotiated clock rate, and caches by content hash. Home
  Assistant already caches TTS by message hash, so repeated phrases hit the cache and skip both
  the fetch and the transcode.
- `ffprobe` for duration, used for the playback timeout rather than a progress bar.

## Phase 3: the integration — **built**

```
custom_components/pbx_page/
  __init__.py            # setup, sidecar client lifecycle
  manifest.json          # domain pbx_page, iot_class local_push
  config_flow.py         # sidecar URL or MQTT topic, paging targets
  const.py
  client.py              # HTTP + websocket (or MQTT) client, reconnect, event dispatch
  media_player.py
  diagnostics.py
tests/
hacs.json
```

No SIP, no RTP, no compiled dependencies. The integration is a state machine driven by sidecar
events, which is what makes it maintainable.

Config flow: sidecar endpoint (validated against `/health`), then paging targets. Options flow:
lead-in delay, default chime, concurrency policy.

## Phase 4: the entity — **built**

Verified against a real Home Assistant: `supported_features` reads 7040, exactly
`PLAY_MEDIA | STOP | TURN_ON | TURN_OFF | SELECT_SOURCE` and nothing else, and the entity walks
`idle -> buffering -> playing -> idle` on a live page. Pulling the sidecar out from under it turns
it `unavailable`; putting it back recovers without intervention.


One entity per paging target. Advertise exactly:

- `PLAY_MEDIA`
- `STOP` (hang up)
- `TURN_ON` / `TURN_OFF` (soft enable and disable)
- `SELECT_SOURCE` only if static sounds are exposed that way

Do not advertise `PAUSE`, `SEEK`, `VOLUME_SET`, `BROWSE_MEDIA`, or track navigation. For clips of
a few seconds those are theatre, and advertising features the backend cannot honour breaks
automations that trust them.

`async_play_media`: resolve via `media_source` and `async_process_play_media_url` unless the id
matches a static sound, then `POST /call`. Treat `announce: true` as ordinary playback, since
there is nothing to duck or resume on a page and raising would break `tts.speak` callers that set
it by default.

State mapping, driven entirely by sidecar events:

| Sidecar event | State |
| --- | --- |
| Disabled by user | `off` |
| No active call | `idle` |
| `calling` or `early` | `buffering` |
| `playback_started` | `playing` |
| `disconnected` | `idle` |
| Sidecar unreachable or account unregistered | `unavailable` |

`unavailable` on lost registration matters. A page group that silently stops working is worse than
one that visibly breaks, and it gives you something to alert on.

## Phase 5: concurrency — **built**

Queueing lives in the integration, where entity semantics are; the sidecar implements only
`reject` and `preempt`. `pbx_page.page` ships with a `priority` field, and `urgent` forces preempt
for the alarm chain.


Per entity:

- `queue` (default): serialise, bounded depth of about 3, drop oldest on overflow.
- `preempt`: hang up and re-originate. **Use this for the smoke, leak, and siren chain**, where
  the newest message is the one that matters. A routine announcement must never delay an alarm.
- `reject`: fail fast if busy.

A global lock across entities sharing physical handsets, as an explicit option rather than an
inferred one. Note that a busy page group now returns 486 rather than needing inference, so
`reject` is cheap and correct to implement.

Also ship `pbx_page.page(text | sound, targets, chime, priority)` for automations that do not need
media player semantics.

## Phase 6: hardening

- **Diagnostics: done.** The sidecar keeps the last twenty finished calls and serves them at
  `GET /calls/history`; the integration folds them into its diagnostics download along with
  registration state and every entity's state.

  Two things were added beyond the sketch. **Latency is split** into answer latency (the PBX's)
  and playback latency (ours, plus the lead-in) — a page that feels slow is one or the other, and
  a single total cannot say which. And each record carries **`audio_sent`**, from the RTP packet
  counters, because a call that connects and sends nothing is indistinguishable from a working one
  by its SIP dialog alone.

  On redaction: SIP credentials never leave the sidecar, and the media label is a content hash
  plus a host rather than a URL. A Home Assistant TTS proxy URL carries a token that grants access
  to the audio, and diagnostics files get shared around.
- **Tests: done.** 112 across three suites ([`docs/testing.md`](../docs/testing.md)):
  47 integration-side against a mocked sidecar, covering every case named here; 52 sidecar-side
  unit tests; and 13 end-to-end against a real Asterisk 22.10.1 in Docker — the same version the
  production FreePBX runs, and needing nothing FreePBX-specific, exactly as predicted.

  The end-to-end suite records what the far end hears, so "did the page arrive" is measured rather
  than assumed. It is what verifies the lead-in: page with `lead_in: 1.0` and the recording's first
  sound really is at 1.0 s, silence before it.

  Writing them found four defects that live testing had missed:

  1. **The entity reported `idle` mid-call.** The sidecar emits `calling` while the POST is still
     in flight, so the integration had not yet learnt the call id and filtered its own first event
     out. Visible in the earlier live runs, and missed.
  2. **The teardown reason was always discarded.** An answer timeout, a preemption and a normal
     hangup all reported `disconnected`, so diagnostics could never say *why* a page failed —
     directly against this phase's own goal.
  3. **Config defaults skipped their type cast**, so `PBX_PORT` set with `SIP_PORT` unset produced
     the string `"5060"` for a field declared `int`.
  4. **`main.py` loaded config at import**, turning a missing environment variable into an import
     traceback and making the app untestable without a full environment.
- **Missing ffmpeg is detected at startup and fails loudly** — the lifespan raises and the process
  exits rather than waiting to discover it at page time. Verified in the unit suite, and observed
  for real when a bad import took the sidecar down on startup instead of silently.
- **Packaging: done.** The repository is both a HACS integration repository (`hacs.json` plus
  `custom_components/`) and a Home Assistant add-on repository (`repository.yaml` plus
  `pbx_page_sidecar/`). The two mechanisms read different files and do not collide, so both halves
  stay versioned together. CI runs hassfest, the HACS action and the add-on linter; tagging a
  release publishes per-architecture images to GHCR.

  The add-on pulls a prebuilt image rather than building, because compiling pjproject on a
  Raspberry Pi would take the better part of an hour.

  **The two halves are released as a pair**, which is enforced rather than remembered. Three files
  declare a version — the integration manifest that HACS reads, the add-on config the supervisor
  reads to pick an image tag, and the sidecar's own `/health`. `scripts/versions.sh` fails if they
  disagree and runs on every push; the release workflow re-checks against the tag and refuses to
  publish a mismatch; and at runtime the integration warns when it finds itself talking to a
  sidecar of a different version. A warning rather than an error: a mismatch usually means half an
  upgrade, and refusing to start would take paging down over something that probably still works.

  One multi-arch image serves both the add-on and plain Docker, rather than the conventional
  per-architecture images with `{arch}` substitution. Same bytes for everyone, one less way for
  architectures to drift.

  **Publishing a GitHub release is the trigger.** `prepare release` bumps the versions, runs the
  tests, tags, and opens a draft; pressing Publish builds the multi-arch image, pushes it to Docker
  Hub and GHCR, verifies the pushed image reports the right version on both architectures, and
  fills in the install notes. A release that fails is **put back to draft** — a published release
  with no image is worse than none, because HACS would offer the integration to everyone paired
  with a sidecar they cannot pull. See [`docs/releasing.md`](../docs/releasing.md).

  One thing the add-on shape forced, worth recording: **built-in sounds moved out of `/data`** to
  `/opt/pbx-page/sounds`. The supervisor mounts `/data` as the add-on's persistent volume, which
  would have shadowed every sound baked into the image. The operator's own clips still go in
  `/data/sounds` and are searched first, so a built-in can be overridden by name.

## Risks

| Risk | Mitigation |
| --- | --- |
| RTP blocked by container networking | Decided in phase 0; host networking or an explicit UDP range |
| `pjproject` build friction | `baresip` as plan B; ha-sip standalone as a v0 spike |
| Registration silently drops | Health endpoint, `unavailable` entity state, alertable |
| One-way audio from codec mismatch | Resolved in phase 1: pin the offer to **PCMU 8 kHz mono** |
| First word clipped | Configurable lead-in, default 1 second |
| Two components to deploy instead of one | Add-on packaging for HAOS; documented compose file otherwise |
| Urgent page queued behind a routine one | `preempt` policy on alarm-driven automations |
| **FreePBX Responsive Firewall IP-bans the sidecar** | Hit this in phase 1: a few failed REGISTERs got `10.1.1.50` banned at the IP level, silently — `OPTIONS` simply stopped being answered, with no SIP error. Whitelist the sidecar's address in FreePBX, and **back off hard on auth failure**: a 401/403 must never be retried in a tight loop, or a mistyped password takes paging down until someone unbans it by hand. |

## Definition of done

**Met, 2026-08-31.** `tts.speak` targeting `media_player.working_zone` (extension **991**) paged
the handsets 1.86 s after the service call, entity walking `idle -> buffering -> playing -> idle`,
with 364 RTP packets and no loss. The only PBX-side change remains the one extension.

The original wording, for the record:

`tts.speak` targeting the paging entity (target extension **991**) makes the handsets
auto-answer and speak the message within three seconds, with no clipped first word, and with the
only PBX-side change being one extension created in the FreePBX GUI. A configured chime plays from
the sidecar cache with no fetch. The entity returns to `idle` after every call, including failed
and abandoned ones, and reports `unavailable` if registration is lost.