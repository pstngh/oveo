#!/bin/sh
set -eu
umask 077

container=translation-agent-staging-app-1
volume=translation-agent-staging_translation-data
network=translation-agent-staging_default
v1_root=/opt/translation-agent-staging
v1_config=/etc/translation-agent
v1_run=/run/translation-agent
v2_compose=/opt/oveo/compose.yml
v2_runtime=/etc/oveo/runtime.env
v2_backup=/etc/oveo/backup.env
staging_validator=/usr/local/lib/oveo/validate_staging.py
rehearsal=/opt/oveo/rehearsal.env

die() {
  echo "remove-v1: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ "$#" -eq 3 ] || die "usage: $0 ghcr.io/pstngh/oveo@sha256:DIGEST --confirm REMOVE_V1"
image=$1
[ "$2" = "--confirm" ] && [ "$3" = "REMOVE_V1" ] || die "explicit confirmation is required"
printf '%s\n' "$image" | grep -Eq '^ghcr\.io/pstngh/oveo@sha256:[0-9a-f]{64}$' \
  || die "candidate must be an immutable image from ghcr.io/pstngh/oveo"
install -d -m 0700 /run/lock
exec 9>/run/lock/oveo-maintenance.lock
flock -w 600 9 || die "timed out waiting for the host maintenance lock"

# Refuse removal until every replacement prerequisite is already staged outside v1 paths.
[ -f "$v2_compose" ] || die "$v2_compose is not staged"
[ -x "$staging_validator" ] || die "$staging_validator is not installed"
[ -f "$rehearsal" ] && [ ! -L "$rehearsal" ] || die "$rehearsal is not staged safely"
grep -Fx "OVEO_REHEARSED_IMAGE=$image" "$rehearsal" >/dev/null \
  || die "candidate does not match the exact image that passed the disposable rehearsal"
for file in "$v2_runtime" "$v2_backup" /etc/oveo/backup.agekey; do
  [ -f "$file" ] && [ ! -L "$file" ] || die "$file is not staged as a regular file"
done
[ "$(stat -c '%U:%G:%a' "$v2_runtime")" = "root:root:600" ] \
  || die "$v2_runtime must be root:root mode 0600"
[ "$(stat -c '%U:%G:%a' "$v2_backup")" = "root:root:600" ] \
  || die "$v2_backup must be root:root mode 0600"
python3 "$staging_validator" --runtime "$v2_runtime" \
  || die "staged v2 credentials did not pass content-free validation"
grep -Eq '^AGE_RECIPIENT=age1[0-9a-z]+$' "$v2_backup" \
  || die "a native age recipient is not configured"
[ -s /etc/oveo/backup.agekey ] || die "age identity is empty"
[ "$(stat -c '%U:%G:%a' /etc/oveo/backup.agekey)" = "root:root:400" ] \
  || die "age identity must be root:root mode 0400"
command -v age-keygen >/dev/null 2>&1 || die "age-keygen is not installed"
staged_recipient=$(sed -n 's/^AGE_RECIPIENT=//p' "$v2_backup")
[ "$(age-keygen -y /etc/oveo/backup.agekey)" = "$staged_recipient" ] \
  || die "age recipient does not match the staged identity"
[ -f /etc/caddy/Caddyfile ] || die "Caddy configuration is missing"
grep -F '87.106.103.162' /etc/caddy/Caddyfile >/dev/null \
  || die "the trusted public IP site is not present in Caddy"
grep -F 'reverse_proxy 127.0.0.1:8000' /etc/caddy/Caddyfile >/dev/null \
  || die "the expected loopback reverse proxy is not present"
docker manifest inspect "$image" >/dev/null || die "the published replacement image is not readable"

# Resolve and validate every old Docker object before deleting any of them.
[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$container")" \
  = "translation-agent-staging" ] || die "unexpected compose project on $container"
[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$container")" \
  = "app" ] || die "unexpected compose service on $container"
docker inspect -f '{{range .Mounts}}{{.Name}}:{{.Destination}}{{println}}{{end}}' "$container" \
  | grep -Fx "$volume:/data" >/dev/null || die "old data volume mount does not match inventory"
[ "$(docker volume inspect -f '{{.Name}}' "$volume")" = "$volume" ] \
  || die "old volume does not match inventory"
for id in $(docker ps -aq --no-trunc); do
  [ "$id" = "$(docker inspect -f '{{.Id}}' "$container")" ] && continue
  if docker inspect -f '{{range .Mounts}}{{.Name}}{{println}}{{end}}' "$id" | grep -Fx "$volume" >/dev/null; then
    die "$volume is unexpectedly mounted by another container"
  fi
done
[ "$(docker network inspect -f '{{index .Labels "com.docker.compose.project"}}' "$network")" \
  = "translation-agent-staging" ] || die "old network does not match inventory"

image_ids=$(docker image ls --filter reference='translation-agent:*' --quiet --no-trunc | sort -u)
[ -n "$image_ids" ] || die "no exact translation-agent images were found"
for id in $image_ids; do
  revision=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$id")
  printf '%s\n' "$revision" | grep -Eq '^[0-9a-f]{40}$' \
    || die "translation-agent image $id lacks the expected source revision label"
done

for path in "$v1_root" "$v1_config"; do
  [ -d "$path" ] && [ ! -L "$path" ] || die "$path is not the expected real directory"
  [ "$(stat -c '%U' "$path")" = "root" ] || die "$path is not root-owned"
done
[ ! -e "$v1_run" ] || { [ -d "$v1_run" ] && [ ! -L "$v1_run" ]; } \
  || die "$v1_run is not the expected directory"

docker rm --force "$container"
docker volume rm "$volume"
docker network rm "$network"
for id in $image_ids; do
  docker image rm "$id"
done

remove_exact_tree() {
  target=$1
  case "$target" in
    /opt/translation-agent-staging|/etc/translation-agent|/run/translation-agent) ;;
    *) die "refusing unexpected removal target $target" ;;
  esac
  [ ! -e "$target" ] || rm -rf -- "$target"
}

remove_exact_tree "$v1_root"
remove_exact_tree "$v1_config"
remove_exact_tree "$v1_run"

echo "Removed only the validated v1 translation application objects."
echo "Caddy, Noku, Forte/OpenMoHAA, shared Docker state, and build caches were not touched."
