# Deployment

## This installation

Home Assistant is **2026.7.1, Container install** at `http://10.1.2.96:8123` — no
`hassio` component, `config_dir` is `/config`. That settles the question phase 0
left open: **plain Docker is the target here**, and the add-on manifest in
`sidecar/addon/` is portability for other people's installs rather than something
this site will use.

MQTT is loaded in Home Assistant, so a broker exists. The sidecar still speaks
HTTP plus a websocket, because that keeps it deployable with **no external
dependency** — for an announcement system that is worth more than saving a
connection. MQTT remains a plausible second transport; the control API was
designed so adding one touches only the transport layer.

## Networking, which is the part that actually matters

SIP and RTP both advertise an address, and a page group sends media to whatever
the SDP says. Three arrangements work:

### As a Home Assistant add-on

Add this repository under **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
then install **PBX Page Sidecar**. It pulls a prebuilt image rather than building:
the image compiles pjproject from source, which on a Raspberry Pi would take the
better part of an hour. Remove `image:` from `pbx_page_sidecar/config.yaml` to
build locally instead.

The add-on and the HACS integration live in the same repository. The two
mechanisms read different files (`repository.yaml` plus `pbx_page_sidecar/` for
the supervisor, `hacs.json` plus `custom_components/` for HACS) and do not
collide, which keeps both halves versioned together.

Not applicable to this installation — Home Assistant here is a Container install —
but it is what makes the project usable by anyone else.

### Host networking — the right answer on Linux

```sh
docker compose up -d --build
```

`network_mode: host`. The container's address *is* the host's, so nothing has to
be rewritten and the RTP port range needs no mapping. This is also what the add-on
manifest uses (`host_network: true`).

### Bridged — when host networking is unavailable

```sh
docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d
```

This works against FreePBX, and was how the sidecar was first proven. Two separate
mechanisms carry it:

- **SIP** survives because FreePBX honours `rport`/`received`, and pjsua2's contact
  rewriting notices. The log says so explicitly:
  `IP address change detected for account 0 (192.168.215.2:5060 --> 10.1.1.50:5060)`.
- **RTP** survives because FreePBX sets `rtp_symmetric` on extensions by default,
  so the PBX replies to wherever our media actually arrives from rather than to the
  address in our SDP.

If your PBX does neither, set `SIP_PUBLIC_ADDRESS` to an address it can reach; it
is applied to both the SIP transport and the account's media transport.

### macOS with OrbStack — development only

Bridged, as above. **`--network host` is not usable here**: OrbStack serves bind
mounts over a network filesystem that is not reachable from the host network
namespace, so a container started with `--network host` fails to read mounted files
with `OSError: [Errno 5] I/O error`. The image bakes `sounds/` in, so bridged
development needs no mount at all.

## Configuration

Environment variables, or the identically-named lowercase keys in an add-on's
`/data/options.json` (options win when both are present).

| Variable | Default | Notes |
| --- | --- | --- |
| `SIP_USERNAME` / `SIP_EXTENSION` | — | required |
| `SIP_PASSWORD` / `SIP_SECRET` | — | required |
| `SIP_HOST` / `PBX_HOST` | — | required |
| `SIP_PORT` / `PBX_PORT` | `5060` | |
| `SIP_LOCAL_PORT` | `5060` | local SIP port; use 5062 under host networking so it cannot collide |
| `RTP_PORT_START` / `RTP_PORT_COUNT` | `20000` / `100` | |
| `SIP_PUBLIC_ADDRESS` | — | only for bridged behind a PBX without symmetric RTP |
| `SIP_CODECS` | `PCMU/8000/1,PCMA/8000/1` | pinned from the phase 1 measurement |
| `LEAD_IN` | `1.0` | seconds between answer and playback; not optional |
| `MAX_CALL_SECONDS` / `ANSWER_TIMEOUT` | `60` / `20` | guard rails |
| `API_TOKEN` | — | unset means an unauthenticated API; the sidecar warns at startup |
| `SIDECAR_PORT` | `8080` | |
| `LOG_LEVEL` / `SIP_LOG_LEVEL` | `INFO` / `2` | raise `SIP_LOG_LEVEL` to 4 for full SIP traces |
| `SOUNDS_DIR` | `/data/sounds` | your own clips; built-ins ship at `/opt/pbx-page/sounds` and are searched after |
| `SIP_ACCOUNTS` | — | JSON list, for more than one PBX |

`SIP_ACCOUNTS` exists because the plan calls for one account per configured PBX.
Targets are not accounts: one registration pages any number of extensions.

## Do not loop failed registrations

FreePBX's Responsive Firewall IP-banned the development host during phase 1 after
a handful of failed REGISTERs, and the ban is silent — the PBX simply stops
answering, with no SIP error. The sidecar logs auth failures at `ERROR` with this
spelled out, and leans on pjsua2's retry backoff rather than retrying tightly.
Whitelist the sidecar's address on the PBX before putting it into service.
