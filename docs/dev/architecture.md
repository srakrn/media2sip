# Architecture

## Softphone emulation, not AMI

A SIP user agent registers as an ordinary extension and dials the page group.
**The entire PBX-side footprint is one extension created in the GUI** — no
manager user, no dialplan edits, no custom files a FreePBX restore silently
drops. That portability is the point: anything with a SIP registrar works, and
the design must not depend on FreePBX even though that is what it was built
against.

## Two halves

| | |
| --- | --- |
| [`sidecar/`](../../sidecar/) | pjsua2 SIP user agent plus a local HTTP control API. **The only component with a SIP stack.** Ships as a Docker image and, from the same image, as a Home Assistant add-on. |
| [`custom_components/media2sip/`](../../custom_components/media2sip/) | the Home Assistant integration. No SIP, no RTP, no compiled dependencies — a state machine driven by sidecar events. |

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
`media2sip_sidecar/` for the supervisor, `hacs.json` plus `custom_components/` for
HACS — and do not collide. Keeping both here is what keeps the two halves
versioned together.

## Constraints worth knowing before you change something

- **Narrowband, pinned.** The measured negotiation against the page group is
  PCMU 8 kHz mono, ptime 20, with no G.722 on offer, so every clip is transcoded
  to 8 kHz mono 16-bit PCM and the codec list is pinned rather than left to
  pjsua2's defaults. Offering codecs the PBX will never pick only widens the
  surface for a one-way-audio bug.
- **The lead-in is not optional.** Auto-answering handsets need about a second
  after answering before audio is heard. Padding that silence into the clip
  instead works, but is the wrong place for it — every dynamic TTS clip would
  need re-encoding just to prepend it.
- **A page group may never send `180 Ringing`.** It answers immediately on behalf
  of its members, so the normal call goes `calling -> confirmed` with no `early`
  phase. Do not make `early` a required step.
- **RTP counters are the only honest success signal.** A call that connects and
  sends nothing is invisible from the SIP dialog alone, which is why `audio_sent`
  exists and why the end-to-end suite records what the far end actually hears
  ([testing.md](testing.md)).
- **Failed registrations are dangerous.** FreePBX IP-bans on repeated auth
  failure, silently. Never loop them.

## Repository layout

```
custom_components/media2sip/   the Home Assistant integration
sidecar/                      the SIP user agent and control API
media2sip_sidecar/             add-on manifest and user-facing add-on docs
tests/                        integration suite (mocked sidecar)
scripts/                      bump-version.sh, versions.sh
docs/user/                    documentation for people running it
docs/dev/                     this
```

## Verified against

| | |
| --- | --- |
| PBX | FreePBX 17.0.19.32, Asterisk 22.10.1, `chan_pjsip` |
| Media | PCMU 8 kHz mono, ptime 20; G.722 not offered |
| Home Assistant | 2026.7.2, Container install |
| End-to-end suite | Asterisk 22.10.1 in Docker, so the same stack CI exercises |

Nothing in the design depends on FreePBX, but that is what the specifics above —
the narrowband pin, `rport`/`received` and `rtp_symmetric` in
[networking.md](../user/networking.md), the responsive-firewall warning — were
measured against.
