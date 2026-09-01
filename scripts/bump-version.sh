#!/usr/bin/env bash
# Set the version everywhere at once, so the integration and the Docker image are
# always released as a pair.
#
#   ./scripts/bump-version.sh 0.2.0
#
# Then commit, tag v0.2.0, and push the tag. The release workflow does the rest.
set -euo pipefail
cd "$(dirname "$0")/.."

version="${1:?usage: bump-version.sh <version>, e.g. 0.2.0}"
version="${version#v}"

if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$ ]]; then
  echo "not a version: $version" >&2
  exit 1
fi

python3 - "$version" <<'PY'
import json, pathlib, re, sys

version = sys.argv[1]

manifest = pathlib.Path("custom_components/pbx_page/manifest.json")
data = json.loads(manifest.read_text())
data["version"] = version
manifest.write_text(json.dumps(data, indent=2) + "\n")

addon = pathlib.Path("pbx_page_sidecar/config.yaml")
addon.write_text(
    re.sub(r'^version: ".*"$', f'version: "{version}"', addon.read_text(), count=1, flags=re.M)
)

main = pathlib.Path("sidecar/app/main.py")
main.write_text(
    re.sub(r'^VERSION = ".*"$', f'VERSION = "{version}"', main.read_text(), count=1, flags=re.M)
)

# The docs and the sample compose pin an image tag; keep them with the rest.
for doc in (
    pathlib.Path("sidecar/docker-compose.example.yml"),
    pathlib.Path("docs/installation.md"),
):
    if doc.is_file():
        doc.write_text(
            re.sub(r"srakrn/media2sip:[0-9][^\s\\]*",
                   f"srakrn/media2sip:{version}", doc.read_text())
        )
print(f"set version to {version}")
PY

./scripts/versions.sh "$version"

cat <<EOF

Next:
  git commit -am "release $version"
  git tag v$version
  git push && git push --tags

The release workflow builds and pushes the sidecar image tagged $version, and
cuts the GitHub release HACS installs the integration from.
EOF
