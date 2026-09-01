#!/usr/bin/env bash
# Read the version each component declares.
#
# Two files declare it, and they must agree: HACS installs the integration from a
# git tag and reads manifest.json, while the supervisor pulls `<image>:<version>`
# using the add-on's own version field. If they drift, a user ends up running an
# integration against a sidecar it was never tested with.
#
# The sidecar's own version is not declared anywhere in its source - it is stamped
# into the image at build time - so there is nothing here to check for it.
set -euo pipefail
cd "$(dirname "$0")/.."

integration=$(python3 -c 'import json;print(json.load(open("custom_components/pbx_page/manifest.json"))["version"])')
addon=$(grep '^version:' pbx_page_sidecar/config.yaml | cut -d'"' -f2)
printf 'integration  %s\n' "$integration"
printf 'add-on       %s\n' "$addon"

if [[ "$integration" != "$addon" ]]; then
  echo "MISMATCH: both must be equal. Run scripts/bump-version.sh <version>." >&2
  exit 1
fi

# The docs and the sample compose pin an image tag. Left to drift they would tell
# a new user to pull a sidecar older than the integration they just installed -
# the exact pairing problem the rest of this script exists to prevent.
stale=$(grep -rl "srakrn/media2sip:[0-9]" docs sidecar --include='*.md' --include='*.yml' 2>/dev/null \
        | xargs grep -L "srakrn/media2sip:$integration" 2>/dev/null || true)
if [[ -n "$stale" ]]; then
  echo "MISMATCH: these pin an image tag other than $integration:" >&2
  echo "$stale" | sed 's/^/  /' >&2
  exit 1
fi

# When invoked with a version (a release tag, minus its leading v), check it too.
if [[ -n "${1:-}" ]]; then
  want="${1#v}"
  if [[ "$want" != "$integration" ]]; then
    echo "MISMATCH: tag says $want, sources say $integration." >&2
    exit 1
  fi
  printf 'tag          %s\n' "$want"
fi

echo "versions agree: $integration"
