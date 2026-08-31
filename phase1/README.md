# Phase 1 rig

Manual proof of concept: register as a SIP extension, dial the paging extension, play a WAV.
See [../docs/phase1-poc.md](../docs/phase1-poc.md) for results.

## Use

```sh
./page.sh                      # register only, no call — safe smoke test
./page.sh 991                  # page 991 with sounds/announce.wav
./page.sh 991 chime_announce   # page 991 with the chime-prefixed clip
LEADIN=2 MAXCALL=30 ./page.sh 991 announce
```

Credentials come from `../.env` (`SIP_EXTENSION`, `SIP_SECRET`, `PBX_HOST`, `PBX_PORT`).
`page.sh` regenerates `baresip/accounts` from it on every run, so that file is git-ignored and
never needs editing by hand.

## Care

**Do not loop failed registrations.** FreePBX's Responsive Firewall IP-banned this host during
setup after a handful of 401s, and the ban is silent — the PBX simply stops answering, with no SIP
error. If `./page.sh` (register only) goes quiet, suspect the ban before suspecting the config.

Every successful run is audible on real office handsets. Prefer `chime` (0.9 s) when you only need
to check that the mechanism still works.

## Layout

```
page.sh            driver: templates the config, dials, hangs up on ffprobe duration
baresip/config     minimal baresip config; audio_source is rewritten per run
baresip/accounts   generated from ../.env — git-ignored
sounds/*.wav       8 kHz mono 16-bit PCM, matching the negotiated PCMU
logs/              git-ignored
```

Regenerate the sounds with `say` + `ffmpeg`; the only hard requirement is 8 kHz mono 16-bit PCM.
