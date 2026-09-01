# Architecture

## Softphone emulation, not AMI

A SIP user agent registers as an ordinary extension and dials the page group.
**The entire PBX-side footprint is one extension created in the GUI** — no
manager user, no dialplan edits, no custom files a FreePBX restore silently
drops. That portability is the point: anything with a SIP registrar works, and
the design must not depend on FreePBX even though that is what it was built
against.

The reasoning in full is in
[`plans/00-master-plan.md`](../../plans/00-master-plan.md).

## Two halves

| | |
| --- | --- |
| [`sidecar/`](../../sidecar/) | pjsua2 SIP user agent plus a local HTTP control API. **The only component with a SIP stack.** Ships as a Docker image and, from the same image, as a Home Assistant add-on. |
| [`custom_components/pbx_page/`](../../custom_components/pbx_page/) | the Home Assistant integration. No SIP, no RTP, no compiled dependencies — a state machine driven by sidecar events. |

The split is what makes the integration maintainable: Home Assistant custom
components cannot reasonably carry a compiled SIP stack, and the half that can is
a container that Home Assistant never has to build.

`app/config.py` reads the supervisor's `/data/options.json` when there is one and
environment variables otherwise, so nothing above it has to know which shape it
is running in.

### Where behaviour lives

- **The sidecar** knows single calls: resolve media, dial, wait for `CONFIRMED`,
  wait the lead-in, play, hang up, record what happened. Its concurrency
  vocabulary is `reject`, `replace`, `preempt`.
- **The integration** knows entities: queueing, cross-target serialisation,
  priorities, media browsing, diagnostics. **Queueing is deliberately not in the
  sidecar** — it needs entity-level semantics.

The control API's full reference — endpoints, the event sequence, the media
pipeline, and the strict single-thread rule that keeps pjsua2 and asyncio from
fighting — is in [`sidecar/README.md`](../../sidecar/README.md).

## Released as a pair

The two halves talk over that control API and are only ever tested together, so
they share one version and are released together. This is enforced rather than
remembered: CI fails on drift, the release workflow refuses a mismatched tag, and
the integration warns at runtime. See [releasing.md](releasing.md).

**No version is hardcoded in any source file.** Two metadata files declare one
because external systems read them out of the repository, and the sidecar's is
stamped into the image at build time.

## One repository, two distribution channels

The add-on store and HACS read different files — `repository.yaml` plus
`pbx_page_sidecar/` for the supervisor, `hacs.json` plus `custom_components/` for
HACS — and do not collide. Keeping both here is what keeps the two halves
versioned together.

## Constraints worth knowing before you change something

- **Narrowband, pinned.** The measured negotiation is PCMU 8 kHz mono, ptime 20,
  with no G.722 on offer ([environment.md](environment.md)). The codec list is
  pinned rather than left to pjsua2's defaults; offering codecs the PBX will
  never pick only widens the surface for a one-way-audio bug.
- **The lead-in is not optional.** Phase 1 established that handsets need about a
  second after answering before audio is heard, and that padding it into the clip
  is the wrong place — every dynamic TTS clip would need re-encoding
  ([phase1-poc.md](phase1-poc.md)).
- **RTP counters are the only honest success signal.** A call that connects and
  sends nothing is invisible from the SIP dialog alone, which is why `audio_sent`
  exists and why the end-to-end suite records what the far end actually hears
  ([testing.md](testing.md)).
- **Failed registrations are dangerous.** FreePBX IP-bans on repeated auth
  failure, silently. Never loop them.

## Repository layout

```
custom_components/pbx_page/   the Home Assistant integration
sidecar/                      the SIP user agent and control API
pbx_page_sidecar/             add-on manifest and user-facing add-on docs
tests/                        integration suite (mocked sidecar)
scripts/                      bump-version.sh, versions.sh
phase1/                       the baresip proof-of-concept rig, kept re-runnable
plans/                        the master plan
docs/user/                    documentation for people running it
docs/dev/                     this
```
