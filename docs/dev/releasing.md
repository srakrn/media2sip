# Releasing

The integration and the Docker image are released **as a pair**, under one
version. That is not a convention to remember — it is enforced.

## Why it has to be enforced

The two halves talk over a control API, and they are only ever tested together.

**No version is hardcoded in any source file.** Two *metadata* files declare one,
and they have to, because external systems read them straight out of the
repository:

| Declared in | Read by | Why it cannot be derived |
| --- | --- | --- |
| `custom_components/pbx_page/manifest.json` | HACS | read from the tree at the release tag |
| `pbx_page_sidecar/config.yaml` | the supervisor | it is what picks `<image>:<version>` |
| `docker-compose.example.yml`, `installation.md` | whoever copies a pinned tag | it is documentation |

The sidecar's own version is **stamped into the image at build time** — a
`VERSION` build argument becomes `APP_VERSION` in the image, and `app/main.py`
reads it. There is no literal in the source to go stale.

An unstamped build — a working tree, a plain `docker compose build` — reports
`dev`, which is the truth about it. The integration recognises that and stays
quiet rather than warning about a mismatch on every start; training someone to
ignore that warning would waste the one time it matters.

`bump-version.sh` writes the two metadata files and the pinned doc tags.
`versions.sh` checks them. The image is `srakrn/media2sip`, one multi-arch
manifest on Docker Hub and GHCR.

If those drift, a user runs an integration against a sidecar it was never tested
with, and the symptom is a behaviour change with no clue that anything moved.

Three things stop that:

- `scripts/versions.sh` fails if they disagree, including the tags pinned in the
  docs — left alone those would tell a new user to pull a sidecar older than the
  integration they just installed. **CI runs it on every push**, so drift is
  caught long before release day.
- The release workflow runs it again against **the tag**, and publishes nothing if
  they disagree — putting the release back to draft rather than leaving it up.
- At runtime the integration compares its own version with the sidecar's `/health`
  and **logs a warning** if they differ. A warning, not an error: a mismatch
  usually means half an upgrade, and refusing to start would take paging down
  over something that probably still works.

## Cutting a release

**Publishing a GitHub release is the trigger.** Everything else follows from it.

### The easy way

1. **Actions → prepare release → Run workflow**, give it `0.2.0`.
   It bumps all three versions, runs the tests, commits, tags `v0.2.0`, and opens
   a **draft** release.
2. Read the generated notes, then press **Publish release**.

That second step is deliberately yours. Publishing is what builds and pushes, so
the workflow stops just short of it.

### By hand

Same thing, if you would rather:

```sh
./scripts/bump-version.sh 0.2.0
git commit -am "release 0.2.0"
git tag v0.2.0
git push && git push --tags
```

then create the release on that tag in the GitHub UI and publish it.

### What publishing does

1. Checks out **the tag** and verifies the metadata files against it — because
   that is what a user actually gets when HACS installs from this release.
2. Builds one **multi-arch** image (`linux/amd64`, `linux/arm64`) and pushes it to
   Docker Hub and GHCR, tagged `0.2.0`, plus `latest` unless it is a prerelease.
3. Runs the pushed image **on both architectures** and asserts each reports
   `0.2.0` — which now also proves the build stamp was applied, not just that the
   right layer was pushed.
4. Prepends the install instructions to the release notes.

Add-on users get the new image automatically, because the supervisor pulls
`<image>:<version>` and the add-on's version was bumped in step one.

### If it fails

The release is **put back to draft**, with the reason and a link to the failed run
prepended to its notes. A published release with no image is worse than no
release at all: HACS would offer the integration to everyone, paired with a
sidecar they cannot pull.

The usual cause is a tag whose sources say a different version. Fix it with
`./scripts/bump-version.sh`, move the tag to the corrected commit, and publish
again.

### Re-running

**Actions → release → Run workflow**, with an existing release. Useful if Docker
Hub was down, or credentials were wrong the first time.

### Version input

Both workflows take `0.2.0` or `v0.2.0` — whichever you type, they mean the same
release. The git tag is always `vX.Y.Z`; `prepare release` creates it, and
`release` resolves either form back to it.

## Image tags

| Tag | Moves | For |
| --- | --- | --- |
| `0.2.0` | never | the add-on pulls this, and it is what to pin in production |
| `0.2` | to the newest patch of `0.2` | bug fixes without behaviour change |
| `1` | to the newest minor of `1` | **only from 1.0 onwards** |
| `latest` | to the newest release | trying it out |

There is deliberately no bare `0` tag. Before 1.0 a minor bump may well break you,
so a tag promising "any 0.x" would be promising something this project cannot
keep. It appears automatically once a major version reaches 1.

A **prerelease moves none of them** — it publishes only its exact version, so it
cannot land on anyone who did not ask for it by name.

Pin `0.2.0` if you run this in earnest. The integration is only tested against the
sidecar it shipped with, and `latest` will eventually hand you a mismatched pair.

## One-time setup

Two repository secrets, under **Settings → Secrets and variables → Actions**:

| Secret | |
| --- | --- |
| `DOCKERHUB_USERNAME` | your Docker Hub account |
| `DOCKERHUB_TOKEN` | an access token from Docker Hub → Account Settings → Personal access tokens, with **Read & Write** |

GHCR needs nothing; the workflow's `GITHUB_TOKEN` covers it.

**If your Docker Hub account is not `srakrn`**, set a repository *variable*
`DOCKERHUB_NAMESPACE` to the right one, and change `image:` in
`pbx_page_sidecar/config.yaml` to match. Those two must agree or the add-on will
pull an image that does not exist.

Nothing appears on Docker Hub until a release is **published**. `prepare release`
only opens a draft, and a draft builds nothing — that is the point of it.

Make the Docker Hub repository public on its first push, or add-on users will get
an authentication error rather than an image.

**GHCR needs the same treatment, and it is easy to miss.** Actions creates the
container package *private*, so an anonymous pull gets a 404 that looks exactly
like "no such image". Flip it once, at
`github.com/users/<you>/packages/container/media2sip/settings` → Change
visibility → Public. Docker Hub is the primary either way; GHCR is the mirror the
docs promise, so it should not be a broken promise.

If your default branch is protected against direct pushes, the **prepare release**
workflow cannot commit the version bump. Either allow the `github-actions[bot]`
actor to bypass it, or do that step by hand — the release itself is unaffected.

## Why a single multi-arch image

Add-ons conventionally publish one image per architecture and let the supervisor
substitute `{arch}`. A multi-arch manifest does the same job with one tag, and it
means plain Docker users and add-on users pull exactly the same bytes. Fewer
images, and one less way for architectures to drift apart.
