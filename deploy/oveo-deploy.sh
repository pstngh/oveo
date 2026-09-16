#!/bin/sh
set -eu
umask 077

repo=ghcr.io/pstngh/oveo
compose_file=/opt/oveo/compose.yml
deploy_env=/etc/oveo/deploy.env
runtime_env=/etc/oveo/runtime.env
staging_validator=/usr/local/lib/oveo/validate_staging.py
data_dir=/var/lib/oveo
public_ready=https://87.106.103.162/health/ready

die() {
  echo "oveo-deploy: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ "$#" -eq 1 ] || die "usage: $0 ghcr.io/pstngh/oveo@sha256:DIGEST"
image=$1
printf '%s\n' "$image" | grep -Eq '^ghcr\.io/pstngh/oveo@sha256:[0-9a-f]{64}$' \
  || die "deployment requires the exact Oveo repository and an immutable sha256 digest"

[ -f "$compose_file" ] || die "$compose_file is missing"
[ -f "$runtime_env" ] && [ ! -L "$runtime_env" ] || die "$runtime_env is missing or unsafe"
[ "$(stat -c '%U:%G:%a' "$runtime_env")" = "root:root:600" ] \
  || die "$runtime_env must be root:root mode 0600"
[ -x "$staging_validator" ] || die "$staging_validator is not installed"
python3 "$staging_validator" --runtime "$runtime_env" \
  || die "staged credentials did not pass content-free validation"

install -d -m 0700 -o 10001 -g 10001 "$data_dir" "$data_dir/attachments"
install -d -m 0700 /etc/oveo /run/lock
exec 9>/run/lock/oveo-maintenance.lock
flock -w 600 9 || die "timed out waiting for the host maintenance lock"

previous=
if [ -f "$deploy_env" ]; then
  previous=$(sed -n 's/^OVEO_IMAGE=//p' "$deploy_env")
fi
if [ -n "$previous" ]; then
  printf '%s\n' "$previous" | grep -Eq '^ghcr\.io/pstngh/oveo@sha256:[0-9a-f]{64}$' \
    || die "recorded previous image is not a valid immutable Oveo image"
fi

docker manifest inspect "$image" >/dev/null \
  || die "immutable candidate is not readable from GHCR"
docker pull "$image"
docker image inspect "$image" --format '{{range .RepoDigests}}{{println .}}{{end}}' \
  | grep -Fx "$image" >/dev/null \
  || die "pulled image does not expose the requested repository digest"

OVEO_IMAGE=$image docker compose --project-name oveo --env-file "$deploy_env" \
  -f "$compose_file" config --quiet 2>/dev/null \
  || OVEO_IMAGE=$image docker compose --project-name oveo -f "$compose_file" config --quiet

write_image_env() {
  selected=$1
  temporary=${deploy_env}.next
  printf 'OVEO_IMAGE=%s\n' "$selected" >"$temporary"
  chmod 0600 "$temporary"
  chown root:root "$temporary"
  mv -f "$temporary" "$deploy_env"
}

wait_ready() {
  attempt=0
  while [ "$attempt" -lt 45 ]; do
    if curl --fail --silent --show-error --max-time 5 \
      http://127.0.0.1:8000/health/ready >/dev/null; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  return 1
}

rollback() {
  [ -n "$previous" ] || return 1
  echo "Candidate failed; restoring the preceding immutable Oveo image." >&2
  docker pull "$previous" >/dev/null || return 1
  write_image_env "$previous" || return 1
  docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" \
    up --detach --remove-orphans app || return 1
  wait_ready \
    && curl --fail --silent --show-error --max-time 15 "$public_ready" >/dev/null
}

write_image_env "$image"
if ! docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" \
  up --detach --remove-orphans app; then
  if rollback; then
    die "candidate failed to start; preceding image was restored"
  fi
  die "candidate failed to start and the preceding image could not be restored"
fi
if ! wait_ready || ! curl --fail --silent --show-error --max-time 15 "$public_ready" >/dev/null; then
  if rollback; then
    die "candidate failed readiness; preceding image was restored"
  fi
  die "candidate failed readiness and the preceding image could not be restored"
fi

printf '%s\n' "$image" >"$data_dir/deployed-image"
chown 10001:10001 "$data_dir/deployed-image"
chmod 0600 "$data_dir/deployed-image"

# Remove only superseded images from this exact GHCR repository. Never prune globally.
docker image ls --digests --format '{{.Repository}}@{{.Digest}}' | sort -u \
  | while IFS= read -r old_image; do
      case "$old_image" in
        "$repo"@sha256:*) ;;
        *) continue ;;
      esac
      [ "$old_image" = "$image" ] && continue
      printf '%s\n' "$old_image" | grep -Eq '^ghcr\.io/pstngh/oveo@sha256:[0-9a-f]{64}$' \
        || continue
      docker image rm "$old_image" >/dev/null 2>&1 || true
    done

docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" ps
echo "Deployed and verified $image"
