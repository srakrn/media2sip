# Testing

Three suites, deliberately separated by what they need to run.

| Suite | Where | Needs | Count |
| --- | --- | --- | --- |
| Integration | `tests/` | a Python venv | 47 |
| Sidecar unit | `sidecar/tests/` | the sidecar image (ffmpeg) | 52 |
| End-to-end | `sidecar/tests/integration/` | Docker, a real Asterisk | 15 |

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

### Three things that will bite you

**Never hardcode the version in a test.** Use the `integration_version` fixture,
which reads the manifest. Nothing in either codebase declares a version literal —
the sidecar's is stamped into its image at build time — so a test that writes one
down is the only place it can go stale.
The release workflow bumps the manifest before running the tests, so a hardcoded
copy fails on every release — which is exactly how the first release attempt
died, at the `test before tagging` step.


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
docker build -t media2sip:latest .
docker build -f Dockerfile.test -t media2sip:test .
docker run --rm media2sip:test
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
measuring the far end's audio rather than by trusting that a call connected. A
call that connects and sends nothing is the failure this project most needs to
catch, and it is invisible from the SIP dialog alone.

**Measure each thing where it is defined.** The obvious lead-in test — page with
`lead_in: 1.0`, check the recording's first sound is at 1.0 s — passes alone and
fails in a suite, which is exactly how it behaved in CI. Asterisk starts writing
the file some way after it answers, and back-to-back that delay was measured at
~0.6 s, subtracting straight off the apparent onset. The recording's zero is the
recorder's, not the call's.

So the lead-in is asserted against the sidecar's own `answer_latency` and
`playback_latency`, which bracket it precisely, and the recording is used for what
it can prove: that the audio arrived **whole**. That comparison is a round trip —
the received audio's audible span against the source clip's, measured the same
way — rather than against the clip's duration, since clips carry trailing silence
and an intact page still sounds shorter than the file is long.

`wait_idle()` waits for both sides, too. Waiting only for the sidecar lets a slow
runner drop one test's recording on top of the next one's, because every test
writes the same filename.
