#!/bin/sh
set -eu
umask 077

config=/etc/oveo/backup.env
tool=/usr/local/lib/oveo/backup_tool.py
compose_file=/opt/oveo/compose.yml
deploy_env=/etc/oveo/deploy.env
live_dir=/var/lib/oveo

die() {
  echo "oveo-restore: $*" >&2
  exit 1
}

usage() {
  echo "usage: $0 BACKUP.age --destination EMPTY_PATH" >&2
  echo "       $0 BACKUP.age --live --confirm RESTORE" >&2
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
  [ "$#" -eq 1 ] || usage
  destination=$1
  case "$destination" in
    /*) ;;
    *) die "destination must be an absolute path" ;;
  esac
  [ "$destination" != / ] && [ "$destination" != "$live_dir" ] \
    || die "use --live for the production data directory"
  [ ! -e "$destination" ] || die "destination already exists"
elif [ "$mode" = "--live" ]; then
  [ "$#" -eq 2 ] && [ "$1" = "--confirm" ] && [ "$2" = "RESTORE" ] || usage
else
  usage
fi

install -d -m 0700 /var/tmp /run/lock
temporary=$(mktemp -d /var/tmp/oveo-restore.XXXXXX)
cleanup() {
  case "$temporary" in
    /var/tmp/oveo-restore.*) rm -rf -- "$temporary" ;;
    *) echo "Refusing unsafe temporary cleanup: $temporary" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM
plain=$temporary/backup.tar.gz
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$plain" "$backup"

if [ -n "$destination" ]; then
  python3 "$tool" restore --archive "$plain" --destination "$destination"
  echo "Restored and verified backup in disposable destination: $destination"
  exit 0
fi

[ -f "$compose_file" ] || die "$compose_file is missing"
[ -f "$deploy_env" ] || die "$deploy_env is missing"
exec 9>/run/lock/oveo-maintenance.lock
flock -w 600 9 || die "timed out waiting for the host maintenance lock"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
candidate=/var/lib/oveo.restore.$stamp
previous=/var/lib/oveo.pre-restore.$stamp
failed=/var/lib/oveo.failed-restore.$stamp
[ ! -e "$candidate" ] && [ ! -e "$previous" ] && [ ! -e "$failed" ] \
  || die "a restore path for this timestamp already exists"
python3 "$tool" restore --archive "$plain" --destination "$candidate"
chown -R 10001:10001 "$candidate"
find "$candidate" -type d -exec chmod 0700 {} +
find "$candidate" -type f -exec chmod 0600 {} +

docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" stop app
mv "$live_dir" "$previous"
mv "$candidate" "$live_dir"
if docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" \
    up --detach app \
  && curl --retry 30 --retry-delay 2 --retry-connrefused --fail --silent --show-error \
    --max-time 5 http://127.0.0.1:8000/health/ready >/dev/null; then
  echo "Live restore succeeded. Pre-restore data retained at: $previous"
  exit 0
fi

echo "Restored data failed readiness; reverting to the pre-restore data." >&2
docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" stop app || true
mv "$live_dir" "$failed"
mv "$previous" "$live_dir"
docker compose --project-name oveo --env-file "$deploy_env" -f "$compose_file" up --detach app
die "live restore failed; rejected data retained at $failed"
