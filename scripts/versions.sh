#!/usr/bin/env bash
# Read the version each component declares.
#
# There are three, and they must agree: HACS installs the integration from a git
# tag and reads manifest.json, while the supervisor pulls `<image>:<version>`
# using the add-on's own version field. If they drift, a user ends up running an
# integration against a sidecar it was never tested with.
set -euo pipefail
cd "$(dirname "$0")/.."

integration=$(python3 -c 'import json;print(json.load(open("custom_components/pbx_page/manifest.json"))["version"])')
addon=$(grep '^version:' pbx_page_sidecar/config.yaml | cut -d'"' -f2)
sidecar=$(grep '^VERSION' sidecar/app/main.py | cut -d'"' -f2)

printf 'integration  %s\n' "$integration"
printf 'add-on       %s\n' "$addon"
printf 'sidecar      %s\n' "$sidecar"

if [[ "$integration" != "$addon" || "$integration" != "$sidecar" ]]; then
  echo "MISMATCH: all three must be equal. Run scripts/bump-version.sh <version>." >&2
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
