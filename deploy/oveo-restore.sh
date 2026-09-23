#!/bin/sh
set -eu
umask 077

config=/etc/oveo/backup.env
tool=/usr/local/lib/oveo/backup_tool.py
compose_file=/opt/oveo/compose.yml
deploy_env=/etc/oveo/deploy.env
live_dir=/var/lib/oveo
# Both are shared with every other service on the host (normally mode 1777): used,
# never created or changed.
lock_dir=/run/lock
scratch_dir=/var/tmp

die() {
  echo "oveo-restore: $*" >&2
  exit 1
}

usage() {
  echo "usage: $0 BACKUP.age --destination EMPTY_PATH [--allow-incomplete]" >&2
  echo "       $0 BACKUP.age --live --confirm RESTORE [--allow-incomplete]" >&2
  exit 2
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ "$#" -ge 3 ] || usage
backup=$1
shift
[ -f "$backup" ] && [ ! -L "$backup" ] || die "backup must be a regular file"
[ -f "$config" ] || die "$config is missing"
[ "$(stat -c '%U:%G:%a' "$config")" = "root:root:600" ] \
  || die "$config must be root:root mode 0600"
# shellcheck disable=SC1090
. "$config"
: "${AGE_IDENTITY_FILE:?AGE_IDENTITY_FILE is required}"
[ -s "$AGE_IDENTITY_FILE" ] || die "age identity is missing or empty"
[ "$(stat -c '%U:%G:%a' "$AGE_IDENTITY_FILE")" = "root:root:400" ] \
  || die "age identity must be root:root mode 0400"
[ -x "$tool" ] || die "restore helper is missing"

mode=$1
shift
destination=
if [ "$mode" = "--destination" ]; then
  [ "$#" -ge 1 ] || usage
  destination=$1
  shift
  case "$destination" in
    /*) ;;
    *) die "destination must be an absolute path" ;;
  esac
  [ "$destination" != / ] && [ "$destination" != "$live_dir" ] \
    || die "use --live for the production data directory"
  [ ! -e "$destination" ] || die "destination already exists"
elif [ "$mode" = "--live" ]; then
  [ "$#" -ge 2 ] && [ "$1" = "--confirm" ] && [ "$2" = "RESTORE" ] || usage
  shift 2
else
  usage
fi
# A backup that lacks attachments is restored only when this is asked for explicitly.
incomplete=
if [ "$#" -eq 1 ] && [ "$1" = "--allow-incomplete" ]; then
  incomplete=--allow-incomplete
  shift
fi
[ "$#" -eq 0 ] || usage

[ -d "$scratch_dir" ] && [ -d "$lock_dir" ] || die "$scratch_dir or $lock_dir is missing"
temporary=$(mktemp -d "$scratch_dir/oveo-restore.XXXXXX")
cleanup() {
  case "$temporary" in
    "$scratch_dir"/oveo-restore.*) rm -rf -- "$temporary" ;;
    *) echo "Refusing unsafe temporary cleanup: $temporary" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM
plain=$temporary/backup.tar.gz
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$plain" "$backup"

if [ -n "$destination" ]; then
  python3 "$tool" restore ${incomplete:+"$incomplete"} --archive "$plain" \
    --destination "$destination"
  echo "Restored and verified backup in disposable destination: $destination"
  exit 0
fi

[ -f "$compose_file" ] || die "$compose_file is missing"
[ -f "$deploy_env" ] || die "$deploy_env is missing"
exec 9>"$lock_dir/oveo-maintenance.lock"
flock -w 600 9 || die "timed out waiting for the host maintenance lock"

compose() {
  docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" "$@"
}

stamp=$(date -u +%Y%m%dT%H%M%SZ)
candidate=$live_dir.restore.$stamp
previous=$live_dir.pre-restore.$stamp
failed=$live_dir.failed-restore.$stamp
[ ! -e "$candidate" ] && [ ! -e "$previous" ] && [ ! -e "$failed" ] \
  || die "a restore path for this timestamp already exists"
python3 "$tool" restore ${incomplete:+"$incomplete"} --archive "$plain" \
  --destination "$candidate"
# Error logs and the deployed-image record describe this host, not the backup, so the
# restored directory keeps the current ones (the pre-restore directory keeps them too).
if [ -d "$live_dir/logs" ]; then
  cp -a -- "$live_dir/logs" "$candidate/logs"
fi
if [ -f "$live_dir/deployed-image" ]; then
  cp -p -- "$live_dir/deployed-image" "$candidate/deployed-image"
fi
# The restored data accepts no writes until the application passed its readiness check.
: >"$candidate/maintenance-mode"
chown -R 10001:10001 "$candidate"
find "$candidate" -type d -exec chmod 0700 {} +
find "$candidate" -type f -exec chmod 0600 {} +

# Downtime starts here: everything written after the backup was taken stays only in
# the pre-restore directory.
compose stop app
mv "$live_dir" "$previous"
mv "$candidate" "$live_dir"
if compose up --detach app \
  && curl --retry 30 --retry-delay 2 --retry-connrefused --fail --silent --show-error \
    --max-time 5 http://127.0.0.1:8000/health/ready >/dev/null; then
  rm -f -- "$live_dir/maintenance-mode"
  echo "Live restore succeeded. Pre-restore data retained at: $previous"
  exit 0
fi

echo "Restored data failed readiness; reverting to the pre-restore data." >&2
compose stop app || true
mv "$live_dir" "$failed"
mv "$previous" "$live_dir"
compose up --detach app
die "live restore failed; rejected data retained at $failed"
