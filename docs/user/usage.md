# Using it

One `media_player` entity per paging target. Install first — see
[installation.md](installation.md) — and set the options described in
[configuration.md](configuration.md).

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

### States

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

## Calling it

```yaml
# Speech. `announce: true` is treated as ordinary playback — there is nothing to
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

What happens when a page arrives while one is already playing. Set per entry in
the options flow.

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
guesswork you can simply tell us instead.

> **Changed in 0.3.0.** The default was `queue`. It is now `replace`, so playing
> something new interrupts what is playing instead of waiting behind it. Set the
> policy back to `queue` in the options flow if the old behaviour suited you.

Queueing lives in the integration rather than the sidecar because it needs
entity-level semantics; the sidecar itself knows only `replace`, `preempt` and
`reject`.

---

When a page does not arrive, [troubleshooting.md](troubleshooting.md).
