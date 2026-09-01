# Networking

This is the part that actually matters. SIP and RTP both advertise an address,
and a page group sends media to whatever the SDP says — so if the container's
address is not one the PBX can reach, the call connects and nobody hears
anything. Three arrangements work.

## Host networking — the right answer on Linux

```sh
docker compose up -d
```

`network_mode: host`. The container's address *is* the host's, so nothing has to
be rewritten and the RTP port range needs no mapping. Set `SIP_LOCAL_PORT=5062`
so the sidecar cannot collide with anything else already on 5060.

## As a Home Assistant add-on

The add-on sets `host_network: true`, so it is the case above with nothing to
configure. It pulls a prebuilt image rather than building: the image compiles
pjproject from source, which on a Raspberry Pi would take the better part of an
hour.

## Bridged — when host networking is unavailable

```sh
docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d
```

This works against FreePBX, and was how the sidecar was first proven. Two
separate mechanisms carry it:

- **SIP** survives because FreePBX honours `rport`/`received`, and pjsua2's
  contact rewriting notices. The log says so explicitly:
  `IP address change detected for account 0 (192.168.215.2:5060 --> 10.1.1.50:5060)`.
- **RTP** survives because FreePBX sets `rtp_symmetric` on extensions by
  default, so the PBX replies to wherever our media actually arrives from rather
  than to the address in our SDP.

If your PBX does neither, set `SIP_PUBLIC_ADDRESS` to an address it can reach; it
is applied to both the SIP transport and the account's media transport.

## macOS with OrbStack — development only

Bridged, as above. **`--network host` is not usable here**: OrbStack serves bind
mounts over a network filesystem that is not reachable from the host network
namespace, so a container started with `--network host` fails to read mounted
files with `OSError: [Errno 5] I/O error`. The image bakes `sounds/` in, so
bridged development needs no mount at all.

## Before you point anything new at the PBX

FreePBX's Responsive Firewall bans a host **at the IP level** after a handful of
failed REGISTERs, and the ban is total and silent: `OPTIONS` stops being answered
entirely, with no SIP error to explain it. This is a first-class operational risk,
not a footnote.

The sidecar logs auth failures at `ERROR` with this spelled out, and leans on
pjsua2's retry backoff rather than retrying tightly. Get the credentials right the
first time, and whitelist the sidecar's address on the PBX before putting it into
service.

## Why HTTP and a websocket, not MQTT

The sidecar speaks HTTP plus a websocket so it carries **no external dependency** —
for an announcement system that is worth more than saving a connection, even where
a broker already exists. MQTT remains a plausible second transport; the control
API was designed so adding one touches only the transport layer.
