#!/bin/sh
set -eu
umask 077

config=/etc/oveo/backup.env
database=/var/lib/oveo/oveo.sqlite3
attachments=/var/lib/oveo/attachments
backup_dir=/var/backups/oveo
tool=/usr/local/lib/oveo/backup_tool.py

die() {
  echo "oveo-backup: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -f "$config" ] || die "$config is missing"
[ "$(stat -c '%U:%G:%a' "$config")" = "root:root:600" ] \
  || die "$config must be root:root mode 0600"
# shellcheck disable=SC1090
. "$config"
: "${AGE_RECIPIENT:?AGE_RECIPIENT is required}"
: "${AGE_IDENTITY_FILE:?AGE_IDENTITY_FILE is required}"
printf '%s\n' "$AGE_RECIPIENT" | grep -Eq '^age1[0-9a-z]+$' \
  || die "AGE_RECIPIENT must be a native age recipient"
[ -s "$AGE_IDENTITY_FILE" ] || die "age identity is missing or empty"
[ "$(stat -c '%U:%G:%a' "$AGE_IDENTITY_FILE")" = "root:root:400" ] \
  || die "age identity must be root:root mode 0400"
[ -f "$database" ] || die "database is missing"
[ -x "$tool" ] || die "backup helper is missing"
for command in age flock python3; do
  command -v "$command" >/dev/null 2>&1 || die "$command is not installed"
done

install -d -m 0700 "$backup_dir" /run/lock
exec 9>/run/lock/oveo-maintenance.lock
flock -w 600 9 || die "timed out waiting for the host maintenance lock"

# An unclean shutdown can leave plaintext only in an exact private staging directory.
# The common maintenance lock proves no legitimate backup is currently using one.
find "$backup_dir" -mindepth 1 -maxdepth 1 -type d -name '.staging.*' -print \
  | while IFS= read -r abandoned; do
      [ "$(dirname "$abandoned")" = "$backup_dir" ] || die "unsafe staging path"
      basename "$abandoned" | grep -Eq '^\.staging\.[A-Za-z0-9]+$' \
        || die "unsafe staging filename"
      rm -rf -- "$abandoned"
    done

staging=$(mktemp -d "$backup_dir/.staging.XXXXXX")
cleanup() {
  case "$staging" in
    "$backup_dir"/.staging.*) rm -rf -- "$staging" ;;
    *) echo "Refusing unsafe staging cleanup: $staging" >&2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

stamp=$(date -u +%Y%m%dT%H%M%SZ)
plain=$staging/oveo-$stamp.tar.gz
encrypted=$staging/oveo-$stamp.tar.gz.age
verified=$staging/verified.tar.gz
final=$backup_dir/oveo-$stamp.tar.gz.age

python3 "$tool" create --database "$database" --attachments "$attachments" --archive "$plain"
age --recipient "$AGE_RECIPIENT" --output "$encrypted" "$plain"
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$verified" "$encrypted"
verify_dir=$staging/verified
python3 "$tool" restore --archive "$verified" --destination "$verify_dir"
chmod 0600 "$encrypted"
mv "$encrypted" "$final"

# Keep the seven newest successful encrypted backups. Only exact Oveo backup names qualify.
find "$backup_dir" -mindepth 1 -maxdepth 1 -type f -name 'oveo-*.tar.gz.age' -print \
  | sort -r | sed -n '8,$p' \
  | while IFS= read -r stale; do
      [ "$(dirname "$stale")" = "$backup_dir" ] || die "unsafe rotation path"
      basename "$stale" | grep -Eq '^oveo-[0-9]{8}T[0-9]{6}Z\.tar\.gz\.age$' \
        || die "unsafe rotation filename"
      rm -f -- "$stale"
    done

echo "Created and verified encrypted backup: $final"
