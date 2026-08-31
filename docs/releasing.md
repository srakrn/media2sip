# Releasing

The integration and the Docker image are released **as a pair**, under one
version. That is not a convention to remember — it is enforced.

## Why it has to be enforced

The two halves talk over a control API, and they are only ever tested together.
Three places declare a version, and they are read by three different things:

| Declared in | Read by |
| --- | --- |
| `custom_components/pbx_page/manifest.json` | HACS, when it installs from a GitHub release |
| `pbx_page_sidecar/config.yaml` | the supervisor, which pulls `<image>:<version>` |
| `sidecar/app/main.py` | `GET /health`, so the integration can compare |

If those drift, a user runs an integration against a sidecar it was never tested
with, and the symptom is a behaviour change with no clue that anything moved.

Three things stop that:

- `scripts/versions.sh` fails if the three disagree. **CI runs it on every push**,
  so drift is caught long before release day.
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

1. Checks out **the tag** and verifies it against all three sources — because
   that is what a user actually gets when HACS installs from this release.
2. Builds one **multi-arch** image (`linux/amd64`, `linux/arm64`) and pushes it to
   Docker Hub and GHCR, tagged `0.2.0`, plus `latest` unless it is a prerelease.
3. Runs the pushed image **on both architectures** and asserts each reports
   `0.2.0` — cheap insurance against publishing a stale layer under a fresh tag.
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

**Actions → release → Run workflow**, with an existing tag. Useful if Docker Hub
was down, or credentials were wrong the first time.

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

Make the Docker Hub repository public on its first push, or add-on users will get
an authentication error rather than an image.

If your default branch is protected against direct pushes, the **prepare release**
workflow cannot commit the version bump. Either allow the `github-actions[bot]`
actor to bypass it, or do that step by hand — the release itself is unaffected.

## Why a single multi-arch image

Add-ons conventionally publish one image per architecture and let the supervisor
substitute `{arch}`. A multi-arch manifest does the same job with one tag, and it
means plain Docker users and add-on users pull exactly the same bytes. Fewer
images, and one less way for architectures to drift apart.
