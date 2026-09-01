# media2sip

Expose SIP paging extensions as Home Assistant media players. `tts.speak` at a
`media_player` entity and the handsets auto-answer and speak the message.

Two components:

- **[`sidecar/`](sidecar/)** — a SIP user agent (pjsua2) plus a local control API,
  as a Docker container or a Home Assistant add-on. The only component with a SIP
  stack.
- **[`custom_components/pbx_page/`](custom_components/pbx_page/)** — the Home
  Assistant integration. No SIP, no compiled dependencies; a state machine driven
  by sidecar events.

The PBX-side footprint is **one ordinary extension created in the GUI**. No
manager user, no dialplan edits, no files a FreePBX restore silently drops. That
portability is the point: anything with a SIP registrar works.

## Getting started

Full walkthrough in **[docs/installation.md](docs/installation.md)**. In short:

1. Create one extension on your PBX and note its number and secret.
2. Run the sidecar — the **PBX Page Sidecar** add-on on Home Assistant OS or
   supervised, otherwise Docker with
   [`sidecar/docker-compose.example.yml`](sidecar/docker-compose.example.yml),
   which pulls the published image and needs no checkout. Check
   `curl -s localhost:8080/health` says `registered: true`.
3. Install the integration through **HACS** as a custom repository
   (`https://github.com/srakrn/media2sip`, type *Integration*), restart, then
   add **PBX Page** and point it at the sidecar.

The same repository serves HACS and the add-on store, so both halves stay
versioned together.

## Versioning

The integration and the sidecar image share one version and are **released as a
pair** — they are only ever tested together. CI fails if the versions drift, the
release workflow refuses to publish a mismatched tag, and the integration logs a
warning at runtime if it finds itself talking to a sidecar of a different
version. Pin the image tag rather than using `latest`.

Releasing is one button: **Actions → prepare release** to bump and open a draft,
then **Publish release** in GitHub, which builds and pushes the image and fills in
the notes. See [docs/releasing.md](docs/releasing.md).

[`docs/deployment.md`](docs/deployment.md) covers networking, which is the part
that actually matters. [`docs/integration.md`](docs/integration.md) covers the
entity, services and concurrency.

## Status

| Phase | |
| --- | --- |
| 1 — manual proof of concept | done ([write-up](docs/phase1-poc.md)) |
| 2 — the sidecar | built ([README](sidecar/README.md)) |
| 3, 4, 5 — integration, entity, concurrency | built ([docs](docs/integration.md)) |
| 6 — hardening | done: 114 tests, per-call diagnostics, HACS + add-on packaging |

The definition of done is met: `tts.speak` at `media_player.working_zone` pages
the handsets 1.86 s after the service call, with no clipped first word.

## Tests

```sh
./.venv/bin/python -m pytest tests/          # integration, against a mocked sidecar
docker run --rm media2sip:test        # sidecar unit
./sidecar/tests/integration/run.sh           # end-to-end against a real Asterisk
```

See [`docs/testing.md`](docs/testing.md).

## Documentation

| | |
| --- | --- |
| [installation.md](docs/installation.md) | HACS, the add-on, and Docker, step by step |
| [deployment.md](docs/deployment.md) | networking, which is the part that actually matters |
| [integration.md](docs/integration.md) | the entity, services, concurrency, diagnostics |
| [releasing.md](docs/releasing.md) | how the two halves stay in step |
| [testing.md](docs/testing.md) | the three test suites |
| [environment.md](docs/environment.md) | what this particular installation looks like |
| [phase1-poc.md](docs/phase1-poc.md) | the proof of concept that started it |

See [`plans/00-master-plan.md`](plans/00-master-plan.md) for the design and the
reasoning behind it, and [`docs/environment.md`](docs/environment.md) for what this
particular installation looks like.

## License

MIT — see [LICENSE](LICENSE).
