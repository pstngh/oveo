#!/bin/sh
set -eu
umask 077

repo=ghcr.io/pstngh/oveo
# The compose file of the running release; a rollback starts the preceding image with it.
compose_file=/opt/oveo/compose.yml
deploy_env=/etc/oveo/deploy.env
runtime_env=/etc/oveo/runtime.env
staging_validator=/usr/local/lib/oveo/validate_staging.py
backup_tool=/usr/local/lib/oveo/backup_tool.py
data_dir=/var/lib/oveo
# While this file exists the application answers state-changing requests with 503.
maintenance_marker=$data_dir/maintenance-mode
# Shared with every other service on the host (systemd keeps it at mode 1777): the
# lock file lives here, but the directory itself is never created or changed.
lock_dir=/run/lock
public_ready=https://oveo.duckdns.org/health/ready

die() {
  echo "oveo-deploy: $*" >&2
  exit 1
}

usage="usage: $0 [--bundle STAGED_BUNDLE_DIRECTORY] ghcr.io/pstngh/oveo@sha256:DIGEST"
[ "$(id -u)" -eq 0 ] || die "must run as root"
# With --bundle, the candidate runs with the staged bundle's compose file and tools, and
# the bundle is installed only once the candidate passed its checks: a failed deployment
# leaves the running release's compose file and host scripts as they were.
bundle=
if [ "$#" -eq 3 ] && [ "$1" = --bundle ]; then
  bundle=$2
  shift 2
fi
[ "$#" -eq 1 ] || die "$usage"
image=$1
candidate_compose=$compose_file
if [ -n "$bundle" ]; then
  case "$bundle" in
    /*) ;;
    *) die "the bundle directory must be an absolute path" ;;
  esac
  for file in compose.yml deploy/install-host.sh deploy/validate_staging.py deploy/backup_tool.py; do
    [ -f "$bundle/$file" ] && [ ! -L "$bundle/$file" ] \
      || die "bundle file is missing or unsafe: $bundle/$file"
  done
  candidate_compose=$bundle/compose.yml
  staging_validator=$bundle/deploy/validate_staging.py
  backup_tool=$bundle/deploy/backup_tool.py
fi
printf '%s\n' "$image" | grep -Eq '^ghcr\.io/pstngh/oveo@sha256:[0-9a-f]{64}$' \
  || die "deployment requires the exact Oveo repository and an immutable sha256 digest"

[ -f "$candidate_compose" ] || die "$candidate_compose is missing"
[ -f "$runtime_env" ] && [ ! -L "$runtime_env" ] || die "$runtime_env is missing or unsafe"
[ "$(stat -c '%U:%G:%a' "$runtime_env")" = "root:root:600" ] \
  || die "$runtime_env must be root:root mode 0600"
[ -f "$staging_validator" ] || die "$staging_validator is not installed"
[ -f "$backup_tool" ] || die "$backup_tool is not installed"
python3 "$staging_validator" --runtime "$runtime_env" \
  || die "staged credentials did not pass content-free validation"

install -d -m 0700 /etc/oveo
[ -d "$lock_dir" ] || die "$lock_dir is missing"
exec 9>"$lock_dir/oveo-maintenance.lock"
flock -w 600 9 || die "timed out waiting for the host maintenance lock"

# A live restore or a failed migrating deployment swaps whole data directories with two
# renames. Interrupted between them, it leaves the data under another /var/lib/oveo.*
# name (installing the host bundle may even recreate an empty /var/lib/oveo). A new,
# empty database would then go live as if everything had been lost, so stop instead.
if [ ! -f "$data_dir/oveo.sqlite3" ]; then
  for sibling in "$data_dir".*; do
    [ -d "$sibling" ] || continue
    die "$data_dir/oveo.sqlite3 is missing but $sibling exists; an interrupted restore or deployment left the data under another name (see OPERATIONS.md)"
  done
fi
install -d -m 0700 -o 10001 -g 10001 "$data_dir" "$data_dir/attachments"

# Only an interrupted deployment leaves the write gate on. Whatever it left running
# must be checked by a person before writes are allowed again (see OPERATIONS.md).
[ ! -e "$maintenance_marker" ] \
  || die "$maintenance_marker exists: an earlier deployment did not finish"

previous=
if [ -f "$deploy_env" ]; then
  previous=$(sed -n 's/^OVEO_IMAGE=//p' "$deploy_env")
fi
if [ -n "$previous" ]; then
  printf '%s\n' "$previous" | grep -Eq '^ghcr\.io/pstngh/oveo@sha256:[0-9a-f]{64}$' \
    || die "recorded previous image is not a valid immutable Oveo image"
  [ -f "$compose_file" ] || die "$compose_file is missing, so the running release could not be restored"
fi

docker manifest inspect "$image" >/dev/null \
  || die "immutable candidate is not readable from GHCR"
docker pull "$image"
docker image inspect "$image" --format '{{range .RepoDigests}}{{println .}}{{end}}' \
  | grep -Fx "$image" >/dev/null \
  || die "pulled image does not expose the requested repository digest"

OVEO_IMAGE=$image docker compose --project-name oveo --env-file "$deploy_env" \
  -f "$candidate_compose" config --quiet 2>/dev/null \
  || OVEO_IMAGE=$image docker compose --project-name oveo -f "$candidate_compose" config --quiet

# The running (or preceding) release and the candidate may use different compose files.
compose() {
  docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" "$@"
}

candidate() {
  docker compose --project-name oveo --env-file "$deploy_env" -f "$candidate_compose" "$@"
}

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

ready() {
  # The public check goes through DNS and the proxy, so it rides out a brief failure
  # rather than rolling back a release that works.
  wait_ready && curl --fail --silent --show-error --max-time 15 \
    --retry 5 --retry-delay 3 --retry-all-errors "$public_ready" >/dev/null
}

start_image() {
  write_image_env "$1" && compose up --detach --remove-orphans app
}

start_candidate() {
  write_image_env "$image" && candidate up --detach --remove-orphans app
}

rollback() {
  [ -n "$previous" ] || return 1
  echo "Candidate failed; restoring the preceding immutable Oveo image." >&2
  # The preceding image normally is still local; pull only if it is not, so a rollback
  # does not depend on the registry being reachable.
  docker image inspect "$previous" >/dev/null 2>&1 || docker pull "$previous" >/dev/null \
    || return 1
  start_image "$previous" || return 1
  ready
}

# Does the candidate migrate the database? The image lists its migrations without
# being started. One that cannot say is treated as migrating, which is always safe.
current_revision=$(python3 "$backup_tool" revision --database "$data_dir/oveo.sqlite3") \
  || die "could not read the database schema revision"
migrating=false
if [ "$current_revision" != none ]; then
  if candidate_revisions=$(docker run --rm --pull never --network none --read-only \
    --user 10001:10001 --entrypoint oveo-admin "$image" \
    schema-revisions --config /app/alembic.ini); then
    candidate_head=$(printf '%s\n' "$candidate_revisions" | sed -n '1p')
    printf '%s\n' "$candidate_head" | grep -Eq '^[A-Za-z0-9_]+$' \
      || die "the candidate reported no migration head"
    if [ "$current_revision" != "$candidate_head" ]; then
      printf '%s\n' "$candidate_revisions" | grep -Fqx -- "$current_revision" \
        || die "the database is at schema revision $current_revision, which the candidate does not know"
      migrating=true
    fi
  else
    echo "The candidate cannot list its migrations; deploying it as one that migrates." >&2
    migrating=true
  fi
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
snapshot_dir=$data_dir.predeploy.$stamp
failed_dir=$data_dir.failed-deploy.$stamp
if [ "$migrating" = true ]; then
  # The previous image cannot run on a migrated database, so a failed candidate is
  # undone by putting back the whole data directory as it was before the migration.
  [ -n "$previous" ] || die "a migrating deployment needs a recorded previous image"
  [ ! -e "$snapshot_dir" ] && [ ! -e "$failed_dir" ] \
    || die "a deployment directory for this timestamp already exists"
  echo "The candidate migrates the database from $current_revision; Oveo is unavailable until the candidate passes its checks." >&2
  # Refuse writes first (a release with the gate honors it at once), then stop the
  # running release so that nothing writes while the data directory is copied.
  : >"$maintenance_marker"
  if ! compose stop app; then
    rm -f -- "$maintenance_marker"
    die "could not stop the running release"
  fi
  if ! python3 "$backup_tool" snapshot --data-dir "$data_dir" --destination "$snapshot_dir"; then
    rm -f -- "$maintenance_marker"
    compose up --detach app || true
    die "could not snapshot the data directory; the running release was restarted unchanged"
  fi
fi

# With the marker in place the candidate serves reads and health checks but accepts no
# writes until every check passed, so undoing it can lose no user data.
if start_candidate && ready; then
  rm -f -- "$maintenance_marker"
else
  if [ "$migrating" = true ]; then
    echo "Candidate failed; putting back the data directory from before its migration." >&2
    candidate stop app || true
    mv -T -- "$data_dir" "$failed_dir" \
      || die "could not set the candidate's data aside; Oveo is stopped (see OPERATIONS.md)"
    mv -T -- "$snapshot_dir" "$data_dir" \
      || die "could not put back $snapshot_dir; Oveo is stopped (see OPERATIONS.md)"
    if rollback; then
      die "candidate failed; the preceding image runs on the pre-deployment data; the candidate's data is kept at $failed_dir"
    fi
    die "candidate failed and the preceding image could not be restored; the pre-deployment data is live and the candidate's data is kept at $failed_dir"
  fi
  if rollback; then
    die "candidate failed to start or pass its checks; preceding image was restored"
  fi
  die "candidate failed to start or pass its checks and the preceding image could not be restored"
fi

printf '%s\n' "$image" >"$data_dir/deployed-image"
chown 10001:10001 "$data_dir/deployed-image"
chmod 0600 "$data_dir/deployed-image"

# The candidate is live, so its compose file and host scripts now describe the host.
if [ -n "$bundle" ]; then
  "$bundle/deploy/install-host.sh" \
    || die "$image is live, but installing its host bundle failed; $compose_file and the host scripts are from the preceding release until the bundle is installed (see OPERATIONS.md)"
fi

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

candidate ps
if [ "$migrating" = true ]; then
  echo "Pre-deployment data kept at $snapshot_dir; remove that exact directory once this release is verified."
fi
echo "Deployed and verified $image"
