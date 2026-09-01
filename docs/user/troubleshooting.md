# Troubleshooting

A page that does not arrive is almost always one of five things. Work down the
list — each step rules out the ones above it.

## 1. Is the sidecar registered?

```sh
curl -s http://<sidecar-host>:8080/health
```

You want `"status": "ok"` and `"registered": true`. `status` is `degraded` when
any account is unregistered.

An unregistered sidecar gives you an entity that is permanently `unavailable` and
no clue why, so fix it here before looking anywhere else. The add-on log should
say:

```
account 9901 registering as sip:9901@10.1.2.99
event registered {'account_id': '9901', 'code': 200, 'reason': 'OK'}
```

**If registration is silently going nowhere, suspect an IP ban.** FreePBX's
Responsive Firewall bans a host at the IP level after a handful of failed
REGISTERs, and the ban is total: the PBX simply stops answering, with no SIP
error. Whitelist the sidecar's address on the PBX. Details in
[networking.md](networking.md).

## 2. Is the entity `unavailable`?

That means the integration cannot reach the sidecar, or a registration was lost.
Check the URL in the config entry, and that Home Assistant can reach the sidecar's
host and port — the two commonly run on different machines.

## 3. Did the call happen?

```sh
curl -s http://<sidecar-host>:8080/calls/history
```

This is the useful one. Each entry carries the target, the **SIP reason code**,
the latency split into the PBX's part and ours, and `audio_sent`.

| What you see | Means |
| --- | --- |
| `486` | the page group was busy |
| `480` / `404` | the paging extension is wrong, or not registered |
| `408`, no answer | nothing picked up within `ANSWER_TIMEOUT` |
| connected, `audio_sent: 0` | the call worked and **no RTP went down the wire** |

That last row is the one to look at first. A call that connects and sends no RTP
is indistinguishable from a working page by its SIP dialog alone, so the packet
counters are the only honest signal that a page was actually heard. It usually
means the address the sidecar advertised is not one the PBX can reach — see
[networking.md](networking.md).

## 4. Was the first word clipped?

Raise `LEAD_IN`. Auto-answering handsets need about a second to open the audio
path after they answer, and the sidecar waits that long before starting playback.
Padding silence into the clip instead works, but means re-encoding every dynamic
TTS clip just to prepend it.

## 5. Is one page cutting off another?

That is the concurrency policy doing what it is set to do. `replace` is the
default; change it in the options flow. See
[usage.md](usage.md#concurrency).

## Diagnostics

Download from the device page in Home Assistant. It carries registration state
per account, connection state, every entity's state, and the **last twenty
calls** — target, SIP reason code, latency split, and `audio_sent`. Attach it to
a bug report.

Tokens are redacted, and the sidecar labels media by content hash and host rather
than by URL, so a Home Assistant TTS proxy URL — which carries a token granting
access to the audio — never reaches a diagnostics file that gets shared around.

## Version mismatch in the log

> the sidecar reports a different version

The two halves are only ever tested together. Update whichever is behind; see
[configuration.md](configuration.md#keeping-the-halves-in-step). A sidecar built
from a working tree reports `dev`, which the integration recognises and stays
quiet about.

## More detail

Raise `SIP_LOG_LEVEL` to `4` for full SIP traces, and `LOG_LEVEL` to `DEBUG`.
Both are in [configuration.md](configuration.md).
