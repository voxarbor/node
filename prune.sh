#!/bin/sh
# Removes VoxArbor Node images this node no longer uses: old tags of ghcr.io/voxarbor/node-* and of the speaches STT
# image that the current compose configuration does not reference. Nothing else on the host is touched (other
# images, containers, volumes, build cache). Prints the space freed. Run after a successful update:
#   ./prune.sh            (in the node folder, next to compose.yaml and .env)
#   ./prune.sh --dry-run  (only list what would be removed)
set -eu
cd "$(dirname "$0")"
DOCKER="${DOCKER:-docker}"
DRY=""; [ "${1:-}" = "--dry-run" ] && DRY=1
COMPOSE="${COMPOSE:-$DOCKER compose --env-file .env}"  # override for a custom layout
used=$($COMPOSE config --images 2>/dev/null | sort -u)
[ -n "$used" ] || { echo "ПОМИЛКА: docker compose config не віддав образів, нічого не видаляю" >&2; exit 1; }
before=$(df -B1 --output=used /var/lib/docker 2>/dev/null | tail -1 || echo 0)
n=0
for ref in $($DOCKER images --format '{{.Repository}}:{{.Tag}}' | grep -E '^(ghcr\.io/voxarbor/node-|ghcr\.io/speaches-ai/speaches:)' | grep -v ':<none>$'); do
  echo "$used" | grep -qx "$ref" && continue
  if [ -n "$DRY" ]; then echo "would remove $ref"; n=$((n + 1)); continue; fi
  $DOCKER image rm "$ref" >/dev/null 2>&1 && { echo "removed $ref"; n=$((n + 1)); } || echo "kept $ref (in use by a container)"
done
# untagged leftovers of this project's images only (their source label), never other dangling images
[ -n "$DRY" ] || $DOCKER image prune -f --filter "label=org.opencontainers.image.source=https://github.com/voxarbor/node" >/dev/null 2>&1 || true
after=$(df -B1 --output=used /var/lib/docker 2>/dev/null | tail -1 || echo 0)
if [ -n "$DRY" ]; then echo "$n images would be removed"; else
  echo "$n images removed, freed $(( (before - after) / 1073741824 )) GB (disk use of /var/lib/docker)"; fi
