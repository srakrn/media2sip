# The `pbx_page` integration

One `media_player` entity per paging target. No SIP, no RTP, no compiled
dependencies — the integration is a state machine driven by sidecar events, which
is what makes it maintainable.

## Install

Install through HACS as a custom repository, or copy
`custom_components/pbx_page/` into your Home Assistant `config/` directory. Either
way, restart, then **Settings → Devices & Services → Add Integration → PBX Page**.
Full walkthrough in [installation.md](installation.md). The flow asks for the sidecar's URL and validates it
against `/health`, so a typo shows up there rather than as an entity that is
permanently unavailable. Then add paging targets one at a time.

## The entity

Advertises `PLAY_MEDIA`, `STOP`, `TURN_ON`, `TURN_OFF`, `BROWSE_MEDIA`, `PAUSE`,
`PLAY`, and `SELECT_SOURCE` when the sidecar has static sounds. Not `SEEK`,
volume, or track navigation: the sidecar has no position control, no mixer and no
notion of a playlist, and advertising features the backend cannot honour breaks
automations that trust them.

**Pause** works by disconnecting the player from the call, not by stopping the
audio stream. The call stays up — the far end keeps receiving silence, so it
never sees a media timeout — and a port nobody pulls from does not advance, so
the clip resumes exactly where it stopped. The page group stays seized while
paused, so `MAX_CALL_SECONDS` still applies; a forgotten pause cannot hold the
handsets forever.

`BROWSE_MEDIA` is in the list because it is genuinely honoured, and because the
**Media panel's player picker filters on that feature alone** — without it the
entity never appears there and is reachable only from services and automations.

Browsing offers the sidecar's own sounds first (already transcoded, played with
no fetch), then everything `media_source` has, filtered to audio. Nothing stops
you picking a half-hour album; `MAX_CALL_SECONDS` on the sidecar cuts the page
off at sixty seconds by default, which is the guard rail rather than a promise
that it will sound sensible. Raise it if you genuinely want long playback — and
note it is also what bounds a paused call.

| Situation | State |
| --- | --- |
| Turned off | `off` |
| No active call | `idle` |
| `calling`, `early`, `confirmed` | `buffering` |
| `playback_started` | `playing` |
| `disconnected` | `idle` |
| Sidecar unreachable, or a registration lost | `unavailable` |

`unavailable` on lost registration is the point of the design. A page group that
silently stops working is worse than one that visibly breaks, and this gives you
something to alert on.

## Using it

```yaml
# Speech. `announce: true` is treated as ordinary playback - there is nothing to
# duck or resume on a page, and raising on it would break tts.speak, which sets
# it by default.
action: tts.speak
target:
  entity_id: tts.openai_tts
data:
  media_player_entity_id: media_player.working_zone
  message: "Delivery at the front door."
```

```yaml
# A static sound, straight from the sidecar's cache with no fetch.
action: pbx_page.page
data:
  targets: [media_player.working_zone]
  sound: chime
```

```yaml
# Alarm-driven. `urgent` preempts whatever is playing and skips the queue.
action: pbx_page.page
data:
  targets: [media_player.working_zone]
  sound: evacuate
  priority: urgent
```

`media_player.play_media` also accepts a `media_source://` id, a plain URL, or
`sound:<name>` for a static clip.

## Concurrency

Set per entry in the options flow. Per entity:

- **`replace`** (default) — playing something new swaps the audio on the call
  that is already up. It starts on the next frame, with **no re-dial and no
  lead-in**, because the handsets are already listening; re-originating instead
  would drop the page group and make it answer again, which is both slower and
  audible. This is what a media player does everywhere else in Home Assistant.
- **`queue`** — serialise, bounded at three, drop the oldest on overflow. The
  dropped caller is told, rather than left waiting on a page that will never
  happen. Prefer this where announcements matter more than immediacy and one
  page cutting off another mid-word would be wrong.
- **`preempt`** — hang up and re-originate. **Use this for the smoke, leak and
  siren chain**, where the newest message is the one that matters. A routine
  announcement must never delay an alarm. `priority: urgent` forces it per call.
- **`reject`** — fail fast when busy. Cheap and correct now that a busy page group
  returns a real 486 rather than needing inference.

**Serialise across all targets** is an explicit option, not an inferred one —
enable it when targets share physical handsets. Inferring which targets overlap is
guesswork the user can simply tell us.

Queueing lives here rather than in the sidecar because it needs entity-level
semantics; the sidecar knows `replace`, `preempt` and `reject`.

> **Changed in 0.3.0.** The default was `queue`. It is now `replace`, so playing
> something new interrupts what is playing instead of waiting behind it. Set the
> policy back to `queue` in the options flow if the old behaviour suited you.

## Options

| Option | Default | Notes |
| --- | --- | --- |
| Lead-in | 1.0 s | Wait between the handsets answering and playback. Too short and the first word is clipped. |
| Default chime | none | Played before every announcement. |
| Concurrency policy | `queue` | Above. |
| Serialise across all targets | off | For targets sharing handsets. |

## Diagnostics

Download from the device page. It carries registration state per account,
connection state, every entity's state, and the **last twenty calls** — target,
SIP reason code, latency split into the PBX's part and ours, and `audio_sent`.

That last field is the one to look at first. A call that connects and sends no
RTP is indistinguishable from a working page by its SIP dialog alone, so the
packet counters are the only honest signal that a page was actually heard.

Tokens are redacted, and the sidecar labels media by content hash and host rather
than by URL, so a Home Assistant TTS proxy URL — which carries a token granting
access to the audio — never reaches a diagnostics file that gets shared around.
