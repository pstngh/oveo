#!/bin/sh
set -eu
umask 077

config=/etc/oveo/backup.env
data_dir=/var/lib/oveo
database=$data_dir/oveo.sqlite3
attachments=$data_dir/attachments
backup_dir=/var/backups/oveo
# Backups that lack attachments live apart from the rotation of complete backups, so
# they can never displace one or be mistaken for the newest restorable backup.
incomplete_dir=$backup_dir/incomplete
# Written by oveo-backup-alert when a run fails; a complete backup clears it.
failure_record=$backup_dir/BACKUP-FAILED
tool=/usr/local/lib/oveo/backup_tool.py
# Shared with every other service on the host: used, never created or changed.
lock_dir=/run/lock

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
# How many complete backups to keep (and, apart from them, incomplete ones). Each takes
# about the size of the data on a small shared disk; BACKUP_KEEP in the config overrides it.
keep=${BACKUP_KEEP:-3}
case "$keep" in
  # With a leading zero the shell's arithmetic would read the number as octal.
  "" | *[!0-9]* | 0*) die "BACKUP_KEEP must be a whole number from 1 to 30" ;;
esac
[ "$keep" -ge 1 ] && [ "$keep" -le 30 ] || die "BACKUP_KEEP must be a whole number from 1 to 30"
[ -f "$database" ] || die "database is missing"
[ -x "$tool" ] || die "backup helper is missing"
for command in age flock python3; do
  command -v "$command" >/dev/null 2>&1 || die "$command is not installed"
done

install -d -m 0700 "$backup_dir"
[ -d "$lock_dir" ] || die "$lock_dir is missing"
exec 9>"$lock_dir/oveo-maintenance.lock"
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
# Likewise the hard links to the attachments that the helper keeps beside them.
find "$data_dir" -mindepth 1 -maxdepth 1 -type d -name '.backup-links.*' -print \
  | while IFS= read -r abandoned; do
      [ "$(dirname "$abandoned")" = "$data_dir" ] || die "unsafe link directory path"
      basename "$abandoned" | grep -Eq '^\.backup-links\.[a-z0-9_]+$' \
        || die "unsafe link directory name"
      rm -rf -- "$abandoned"
    done

# At its peak a run holds the encrypted archive, its decrypted copy and the verification
# restore: about three times the data. Refuse up front, leaving room for the rest of the
# host, instead of filling the shared disk partway through.
headroom=$((256 * 1024 * 1024))
data_bytes=$(du -s -b -c -- "$database" "$attachments" | tail -n 1 | cut -f 1)
needed=$((3 * data_bytes + headroom))
free_bytes=$(df --output=avail -B 1 -- "$backup_dir" | tail -n 1 | tr -d ' ')
[ "$free_bytes" -ge "$needed" ] \
  || die "not enough free space in $backup_dir: a backup of $((data_bytes / 1048576)) MiB of data needs about $((needed / 1048576)) MiB free (three times the data plus 256 MiB for the host), and $((free_bytes / 1048576)) MiB are free"

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

# Exit status 3: the archive was written but lacks referenced attachments.
status=0
python3 "$tool" create --allow-incomplete --database "$database" \
  --attachments "$attachments" --archive "$plain" || status=$?
case "$status" in
  0) complete=true ;;
  3) complete=false ;;
  *) die "the backup could not be created" ;;
esac
age --recipient "$AGE_RECIPIENT" --output "$encrypted" "$plain"
# Each plaintext step is removed once the next one exists, so the staging directory
# never holds more than three copies of the data (the host disk is small).
rm -f -- "$plain"
age --decrypt --identity "$AGE_IDENTITY_FILE" --output "$verified" "$encrypted"
verify_dir=$staging/verified
if [ "$complete" = true ]; then
  python3 "$tool" restore --archive "$verified" --destination "$verify_dir"
else
  python3 "$tool" restore --allow-incomplete --archive "$verified" --destination "$verify_dir"
fi
rm -rf -- "$verified" "$verify_dir"
chmod 0600 "$encrypted"

if [ "$complete" = false ]; then
  install -d -m 0700 "$incomplete_dir"
  kept=$incomplete_dir/oveo-INCOMPLETE-$stamp.tar.gz.age
  mv "$encrypted" "$kept"
  find "$incomplete_dir" -mindepth 1 -maxdepth 1 -type f -name 'oveo-INCOMPLETE-*.tar.gz.age' -print \
    | sort -r | sed -n "$((keep + 1)),\$p" \
    | while IFS= read -r stale; do
        [ "$(dirname "$stale")" = "$incomplete_dir" ] || die "unsafe rotation path"
        basename "$stale" | grep -Eq '^oveo-INCOMPLETE-[0-9]{8}T[0-9]{6}Z\.tar\.gz\.age$' \
          || die "unsafe rotation filename"
        rm -f -- "$stale"
      done
  # Fail the run so the failure alert fires; the complete backups were not touched.
  die "INCOMPLETE backup kept at $kept: referenced attachments are missing or damaged (see OPERATIONS.md)"
fi

mv "$encrypted" "$final"

# Keep the newest $keep successful encrypted backups. Only exact Oveo backup names qualify.
find "$backup_dir" -mindepth 1 -maxdepth 1 -type f -name 'oveo-*.tar.gz.age' -print \
  | sort -r | sed -n "$((keep + 1)),\$p" \
  | while IFS= read -r stale; do
      [ "$(dirname "$stale")" = "$backup_dir" ] || die "unsafe rotation path"
      basename "$stale" | grep -Eq '^oveo-[0-9]{8}T[0-9]{6}Z\.tar\.gz\.age$' \
        || die "unsafe rotation filename"
      rm -f -- "$stale"
    done

rm -f -- "$failure_record"
echo "Created and verified encrypted backup: $final"
