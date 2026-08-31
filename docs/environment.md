# Environment

Recorded 2026-08-31 during the phase 1 proof of concept. This supersedes the guesses in the
master plan's phase 0 list for everything it covers.

## PBX

| | |
| --- | --- |
| Product | FreePBX 17.0.19.32 |
| Asterisk | 22.10.1 |
| Address | `10.1.2.99`, SIP UDP `5060` |
| Stack | `chan_pjsip` (single 5060 listener, no chan_sip split) |
| Realm | `asterisk` |
| Digest | MD5 with `qop=auth` and `opaque` |

## Paging

| | |
| --- | --- |
| Paging extension | **991** |
| Behaviour on INVITE | answers immediately with `200 OK` — **no `180 Ringing`** |

The absence of a `180` is the useful finding: the page group auto-answers on behalf of its members,
so the sidecar's state machine goes `calling -> confirmed` with no `early` phase in the normal case.
Handsets are audibly paged. (The Groundwire-on-iPhone question from phase 0 item 4 is still open —
it was not one of the members exercised here.)

Note the correction against the master plan as originally written: the paging target is **991**, not
the 211 recorded from the pre-migration Issabel system.

## SIP user agent

| | |
| --- | --- |
| Extension | **9901**, created in the FreePBX GUI |
| Credentials | `.env` (git-ignored) |
| PBX-side changes | one extension. No dialplan, no manager user, no custom files. |

This is the whole PBX-side footprint the master plan promised, and it held.

## Media

| | |
| --- | --- |
| Negotiated codec | **PCMU 8000 Hz mono**, ptime 20 |
| G.722 | not negotiated |
| RTP | PBX media on `10.1.2.99:18044/18045`, UA on `10.1.1.50:20000-20100` |

**Consequence for the sidecar:** serve narrowband. Transcode every clip to 8 kHz mono 16-bit PCM
WAV; there is no wideband path to be gained here, so the master plan's "G.722 means wideband TTS is
worth serving" branch is closed.

## Home Assistant

| | |
| --- | --- |
| Version | 2026.7.1 |
| Address | `http://10.1.2.96:8123` |
| Install type | **Container** — no `hassio` component, `config_dir` is `/config` |
| TTS | `tts.openai_tts` |
| MQTT | loaded, so a broker exists |

Install type settles the phase 0 packaging question: **plain Docker**, with the add-on manifest
kept as portability for other installs. A broker exists, but the sidecar speaks HTTP plus a
websocket anyway so it carries no external dependency; see [deployment.md](deployment.md).

## Network

The PoC ran from macOS on `10.1.1.50`, a different subnet from the PBX, routed, with no NAT between
them and no STUN/ICE needed (`medianat=` empty). Two-way RTP established on the first try.

A bridged Docker container reaches the PBX fine: SIP survives NAT because FreePBX honours
`rport`/`received` and pjsua2 rewrites its contact accordingly, and RTP survives because FreePBX
sets `rtp_symmetric` on extensions by default. Verified in phase 2 from a container on
`192.168.215.2` NAT'd behind `10.1.1.50`.

## Firewall — read this before pointing anything new at the PBX

FreePBX's Responsive Firewall **banned `10.1.1.50` at the IP level** after a handful of failed
REGISTER attempts (the extension had not been created yet, so the credentials 401'd). The ban was
total and silent: `OPTIONS` stopped being answered entirely, with no SIP error to explain it.

This is a first-class operational risk for the sidecar, not a footnote. See the risk table in the
master plan.
