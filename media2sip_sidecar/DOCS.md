# Media2SIP Sidecar

Registers as an ordinary SIP extension and dials your paging extension on demand,
so Home Assistant can speak through the handsets. It is the half of
[Media2SIP](https://github.com/srakrn/media2sip) that holds the SIP stack; the
`media2sip` integration talks to it over a local HTTP API.

## Before you start

Create **one extension** on your PBX, in its normal GUI. That is the entire
PBX-side change — no dialplan, no manager user, no files a restore might drop.

> **Do not test with the wrong password.** FreePBX's Responsive Firewall bans a
> host at the IP level after a handful of failed registrations, and the ban is
> silent: the PBX simply stops answering, with no error. Get the credentials
> right first, and whitelist this machine on the PBX.

## Configuration

| Option | Notes |
| --- | --- |
| `sip_username` / `sip_password` | The extension you just created. |
| `sip_host` / `sip_port` | Your PBX. Port is usually 5060. |
| `sip_local_port` | The port this add-on binds. Host networking is on, so pick one that does not collide — 5062 is a good default. |
| `lead_in` | Seconds between the handsets answering and playback starting. **Not optional.** Auto-answering handsets need about a second to open the audio path; too low and the first word is clipped. |
| `api_token` | Optional. Leave empty only on a private network. |
| `sounds_dir` | Your own WAVs, e.g. `/share/media2sip_sounds`. Built-in sounds work regardless. |

Start the add-on and check the log. You want:

```
account 9901 registering as sip:9901@10.1.2.99
event registered {'account_id': '9901', 'code': 200, 'reason': 'OK'}
```

Then add the **Media2SIP** integration in Settings → Devices & Services, pointing
at `http://<this machine>:8080`.

## Checking it works

```
curl -s localhost:8080/health
curl -s localhost:8080/calls/history
```

The history is the useful one when a page does not arrive. Each entry carries the
SIP reason code, the latency split into the PBX's part and ours, and
`audio_sent` — because a call that connects and sends no audio looks identical to
a working one from the SIP dialog alone.

## Networking

Host networking is required and enabled. SIP and RTP both advertise an address,
and a page group sends media to whatever the SDP says, so the container's address
has to be the host's. If you must run it bridged, set `sip_public_address` to an
address the PBX can reach.

## Full documentation

Rendered on GitHub, since the supervisor only shows this page:

- [Installation](https://github.com/srakrn/media2sip/blob/main/docs/user/installation.md) — both halves, step by step
- [Configuration](https://github.com/srakrn/media2sip/blob/main/docs/user/configuration.md) — every option, including the ones
  not exposed as add-on options
- [Usage](https://github.com/srakrn/media2sip/blob/main/docs/user/usage.md) — the entity, the services, concurrency
- [Troubleshooting](https://github.com/srakrn/media2sip/blob/main/docs/user/troubleshooting.md) — when a page does not arrive
