# Installation

Two halves, always installed as a pair: the **integration** in Home Assistant,
and the **sidecar** wherever you can run a container. They are released together
under one version, and the integration logs a warning if it finds itself talking
to a sidecar of a different version.

Before either, create **one extension** on your PBX in its normal GUI, and note
its number and secret. That is the whole PBX-side change.

> FreePBX's Responsive Firewall bans a host at the IP level after a handful of
> failed registrations, and the ban is silent — the PBX just stops answering.
> Get the credentials right the first time.

---

## 1. The sidecar

Pick whichever matches your Home Assistant install.

### Home Assistant OS or supervised — the add-on

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add `https://github.com/srakrn/media2sip`.
3. Install **PBX Page Sidecar**, fill in the extension, secret and PBX host, and
   start it.

It pulls a prebuilt image; the supervisor picks the tag from the add-on version,
so the add-on and the integration stay in step by construction.

### Anything else — Docker

Home Assistant Container, or a separate machine. Grab
[`sidecar/docker-compose.example.yml`](../sidecar/docker-compose.example.yml) —
it pulls the published image and needs no checkout:

```sh
mkdir pbx-page && cd pbx-page
curl -O https://raw.githubusercontent.com/srakrn/media2sip/main/sidecar/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
printf 'SIP_PASSWORD=your-extension-secret\n' > .env
mkdir sounds
# edit docker-compose.yml: SIP_USERNAME, SIP_HOST, and the image tag
docker compose up -d
```

Or without compose:

```sh
docker run -d --name pbx-page-sidecar --restart unless-stopped \
  --network host \
  -e SIP_USERNAME=9901 -e SIP_PASSWORD=secret -e SIP_HOST=10.1.2.99 \
  -e SIP_LOCAL_PORT=5062 -e LEAD_IN=1.0 \
  -v pbx-page-cache:/data/cache \
  srakrn/media2sip:0.2.0
```

Images are on Docker Hub as `srakrn/media2sip` and on GHCR as
`ghcr.io/srakrn/media2sip`, `linux/amd64` and `linux/arm64`.
**Pin the tag** to the integration version rather than using `latest`.

### Check it before going further

```sh
curl -s http://<sidecar-host>:8080/health
```

You want `"status": "ok"` and `"registered": true`. If registration failed, fix
it here — an unregistered sidecar gives you an entity that is permanently
unavailable and no clue why.

Networking gotchas, and the bridged fallback, are in
[deployment.md](deployment.md).

---

## 2. The integration

### Through HACS (recommended)

HACS does not carry this by default, so add it as a custom repository once:

1. Open **HACS** in the sidebar.
2. **⋮ (top right) → Custom repositories**.
3. Repository: `https://github.com/srakrn/media2sip`. Type: **Integration**.
   **Add**.
4. Search HACS for **PBX Page** and **Download**.
5. **Restart Home Assistant.** HACS copies files but does not load new
   integrations until a restart.

Updates then appear in HACS like any other. When one does, update the sidecar to
the matching version too.

It shows a generic icon rather than its own. Integration icons live in
[home-assistant/brands](https://github.com/home-assistant/brands), which is only
needed to get into the HACS default store; a custom repository does not require
it, and nothing else is affected.

> Don't have HACS? Install it from [hacs.xyz](https://hacs.xyz/docs/use/download/download/),
> or use the manual method below — HACS is only a convenience here.

### Manually

Copy the folder into your Home Assistant configuration directory, so you end up
with `config/custom_components/pbx_page/manifest.json`, then restart:

```sh
git clone https://github.com/srakrn/media2sip
cp -r media2sip/custom_components/pbx_page /path/to/config/custom_components/
```

### Configure it

**Settings → Devices & Services → Add Integration → PBX Page**.

Give the sidecar's address (`http://<sidecar-host>:8080`, and the API token if
you set one). It is validated against `/health` before anything is created, so a
typo fails here rather than becoming a permanently unavailable entity. Then add
your paging targets, one at a time — a name and the paging extension.

You get one `media_player` per target. Try it:

```yaml
action: tts.speak
target:
  entity_id: tts.your_engine
data:
  media_player_entity_id: media_player.working_zone
  message: "Testing the paging system."
```

If nothing is heard, `GET /calls/history` on the sidecar tells you why — see
[integration.md](integration.md#diagnostics).

---

## Upgrading

Move both halves together:

1. Update the integration in HACS, restart Home Assistant.
2. Update the sidecar: bump the tag in your compose file and
   `docker compose up -d`, or update the add-on.

Order does not matter much, and a brief mismatch is survivable — but the
integration will say so in the log until both agree.
