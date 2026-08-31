# Testing

Three suites, deliberately separated by what they need to run.

| Suite | Where | Needs | Count |
| --- | --- | --- | --- |
| Integration | `tests/` | a Python venv | 47 |
| Sidecar unit | `sidecar/tests/` | the sidecar image (ffmpeg) | 52 |
| End-to-end | `sidecar/tests/integration/` | Docker, a real Asterisk | 13 |

Nothing anywhere depends on FreePBX, or on your PBX being reachable.

## Continuous integration

| Workflow | Runs on | Does |
| --- | --- | --- |
| `tests.yml` | every push and PR | all three suites below |
| `validate.yml` | every push, PR, and weekly | version consistency, hassfest, HACS, add-on config |
| `prepare-release.yml` | manual | bumps versions, tests, tags, opens a draft release |
| `release.yml` | publishing a release | builds and pushes the image, fills in the notes |

`validate.yml` skips the HACS **brands** check. Brand assets live in
[home-assistant/brands](https://github.com/home-assistant/brands) and exist to get
an integration into the HACS default store; this one is installed as a custom
repository, so the check can never pass from here and does not need to. The only
consequence is a default icon.

The weekly run on `validate.yml` is there because those checks depend on things
outside the repository — Home Assistant's manifest rules and HACS's own — which
can start failing without anything here changing.

## Integration suite

The entity, the concurrency policies, the config flow, the client and
diagnostics, against a **fake sidecar**. This is where the failure modes live —
486 busy, 480 unavailable, registration lost mid-call, queue overflow — because
they are the cases you cannot rehearse on real handsets.

```sh
python3 -m venv .venv
./.venv/bin/pip install "pytest-homeassistant-custom-component==0.13.346"
./.venv/bin/python -m pytest tests/
```

Pin the framework to the Home Assistant version you target;
`0.13.346` is HA 2026.7.2.

### Two things that will bite you

`hass.async_block_till_done()` waits for the entity's queue worker to finish the
page it is on. Call it while a page is deliberately held open and it blocks for
the full call timeout — 32 seconds per test. Use `settle()` from `conftest.py`
instead, which advances the loop without draining. This is the difference between
the suite taking 0.5 seconds and taking 8 minutes.

Home Assistant strips `extra_state_attributes` while an entity is `unavailable`,
so assert on attributes before or after, never during.

## Sidecar unit suite

Media resolution and the content-hash cache, the event bus, the configuration
loader, and the control API's contract with the SIP worker stubbed. Runs inside
the sidecar image, so ffmpeg and pjsua2 are exactly the ones production uses.

```sh
cd sidecar
docker build -t pbx-page-sidecar:latest .
docker build -f Dockerfile.test -t pbx-page-sidecar:test .
docker run --rm pbx-page-sidecar:test
```

## End-to-end suite

The sidecar against a **real Asterisk** — the same 22.10.1 the production FreePBX
runs — on a private Docker network. This is the only suite that exercises
registration, SDP negotiation, RTP and the SIP failure codes for real.

```sh
./sidecar/tests/integration/run.sh          # up, test, down
KEEP=1 ./sidecar/tests/integration/run.sh   # leave the stack running
```

The dialplan gives three targets: **991** answers immediately and records what it
hears, **992** is always busy (486), and **993** rings forever (answer timeout).
The last two exist so the call history can be checked for what it says about
failures, not just successes.

Recording is what makes this worth having. "Did the page arrive" is answered by
measuring the far end's audio — level, and where sound starts — rather than by
trusting that a call connected. That is how the lead-in is verified: page with
`lead_in: 1.0` and the recording's first sound really is at 1.0 s, silent before.
A call that connects and sends nothing is the failure this project most needs to
catch, and it is invisible from the SIP dialog alone.
