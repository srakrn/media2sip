# Configuration

Every setting on both halves. The sidecar is configured where it runs; the
integration is configured in the Home Assistant UI.

## The sidecar

Environment variables, or the identically-named lowercase keys in an add-on's
`/data/options.json`. **Options win when both are present**, so the same image
serves a Docker deployment and an add-on without knowing which it is.

### Required

| Variable | Add-on option | Notes |
| --- | --- | --- |
| `SIP_USERNAME` / `SIP_EXTENSION` | `sip_username` | the extension you created on the PBX |
| `SIP_PASSWORD` / `SIP_SECRET` | `sip_password` | its secret |
| `SIP_HOST` / `PBX_HOST` | `sip_host` | your PBX |

### Everything else

| Variable | Default | Notes |
| --- | --- | --- |
| `SIP_PORT` / `PBX_PORT` | `5060` | |
| `SIP_LOCAL_PORT` | `5060` | local SIP port; use `5062` under host networking so it cannot collide with a softphone or another UA |
| `RTP_PORT_START` / `RTP_PORT_COUNT` | `20000` / `100` | |
| `SIP_PUBLIC_ADDRESS` | — | only for bridged behind a PBX without symmetric RTP; see [networking.md](networking.md) |
| `SIP_CODECS` | `PCMU/8000/1,PCMA/8000/1` | pinned narrowband; offering codecs the PBX will never pick only widens the surface for a one-way-audio bug |
| `LEAD_IN` | `1.0` | seconds between the handsets answering and playback. **Not optional** — auto-answering handsets need about a second to open the audio path, and too low clips the first word |
| `MAX_CALL_SECONDS` / `ANSWER_TIMEOUT` | `60` / `20` | guard rails; `MAX_CALL_SECONDS` is also what bounds a paused call |
| `API_TOKEN` | — | unset means an unauthenticated API, and the sidecar warns loudly at startup. Only acceptable on a private network |
| `SIDECAR_PORT` | `8080` | |
| `LOG_LEVEL` / `SIP_LOG_LEVEL` | `INFO` / `2` | raise `SIP_LOG_LEVEL` to `4` for full SIP traces |
| `SOUNDS_DIR` | `/data/sounds` | your own clips. Built-ins ship at `/opt/media2sip/sounds` and are searched after, so you can override one by name |
| `SIP_ACCOUNTS` | — | JSON list, for more than one PBX |

`SIP_ACCOUNTS` exists because one account is one PBX. **Targets are not
accounts**: a single registration pages any number of extensions.

### Your own sounds

Drop 8 kHz mono 16-bit PCM WAVs into `SOUNDS_DIR` — `/share/media2sip_sounds` is
a sensible choice for the add-on. They appear in `GET /sounds`, in the entity's
source list, and as `sound:<name>` in `media2sip.page`. Other formats are
transcoded on first use, so the requirement is a convention rather than a rule.

## The integration

Set per config entry in the options flow: **Settings → Devices & Services → PBX
Page → Configure**.

| Option | Default | Notes |
| --- | --- | --- |
| Lead-in | 1.0 s | Overrides the sidecar's, per entry. Too short and the first word is clipped. |
| Default chime | none | Played before every announcement. |
| Concurrency policy | `replace` | What happens when a page arrives while one is playing — see [usage.md](usage.md#concurrency). |
| Serialise across all targets | off | For targets that share physical handsets. |

Paging targets are added one at a time, each a name and a paging extension, and
each becomes one `media_player`.

## Keeping the halves in step

The integration and the sidecar image share one version and are only ever tested
together. **Pin the image tag** to the integration's exact version rather than
`latest`, and move both halves at once. The integration logs a warning if it
finds itself talking to a sidecar of a different version; it is a warning rather
than an error because a mismatch usually means half an upgrade, and refusing to
start would take paging down over something that probably still works.
