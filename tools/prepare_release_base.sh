#!/usr/bin/env bash
# Reuse a pinned runtime, flattening exhausted layer stacks with metadata verification.
set -euo pipefail
name=$1
base=$2
tag=$3
[[ "$name" =~ ^[a-z0-9-]+$ ]]
[[ "$base" =~ @sha256:[a-f0-9]{64}$ ]]
root="/workspace/release-base-$name"
docker pull "$base"
docker inspect "$base" > "$root-before.json"
layers=$(docker inspect --format '{{ len .RootFS.Layers }}' "$base")
if (( layers < 100 )); then
  docker tag "$base" "$tag"
  echo "RUNTIME_BASE_REUSED $name layers=$layers"
  exit 0
fi
docker run --rm --network=none --user=0 -v /workspace:/workspace --entrypoint python "$base" \
  /workspace/tools/flatten_runtime.py "$root-before.json" "$root-import.sh" \
  --import-archive "$root.tar" --import-tag "$tag"
container=$(docker create "$base")
trap 'docker rm "$container" >/dev/null; rm -f "$root.tar"' EXIT
docker export --output "$root.tar" "$container"
bash "$root-import.sh"
docker inspect "$tag" > "$root-after.json"
docker run --rm --network=none -v /workspace:/workspace:ro --entrypoint python "$tag" \
  /workspace/tools/flatten_runtime.py "$root-before.json" "$root-after.json" --verify
echo "RUNTIME_BASE_FLATTENED $name original_layers=$layers"
