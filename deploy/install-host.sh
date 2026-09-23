#!/bin/sh
set -eu
umask 022

die() {
  echo "install-host: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
[ "$#" -eq 0 ] || die "usage: $0"
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

for file in \
  "$root/compose.yml" \
  "$root/deploy/oveo-deploy.sh" \
  "$root/deploy/oveo-backup.sh" \
  "$root/deploy/oveo-restore.sh" \
  "$root/deploy/oveo-backup-alert.sh" \
  "$root/deploy/backup_tool.py" \
  "$root/deploy/validate_staging.py" \
  "$root/deploy/runtime.env.example" \
  "$root/deploy/backup.env.example" \
  "$root/REHEARSAL" \
  "$root/deploy/systemd/oveo-backup.service" \
  "$root/deploy/systemd/oveo-backup-failure.service" \
  "$root/deploy/systemd/oveo-backup.timer"; do
  [ -f "$file" ] && [ ! -L "$file" ] || die "bundle file is missing or unsafe: $file"
done

install -d -m 0755 /opt/oveo /usr/local/lib/oveo
install -d -m 0700 /etc/oveo /var/backups/oveo
install -d -m 0700 -o 10001 -g 10001 \
  /var/lib/oveo /var/lib/oveo/attachments /var/lib/oveo/logs
install -m 0644 "$root/compose.yml" /opt/oveo/compose.yml
install -m 0755 "$root/deploy/oveo-deploy.sh" /usr/local/sbin/oveo-deploy
install -m 0755 "$root/deploy/oveo-backup.sh" /usr/local/sbin/oveo-backup
install -m 0755 "$root/deploy/oveo-restore.sh" /usr/local/sbin/oveo-restore
install -m 0755 "$root/deploy/oveo-backup-alert.sh" /usr/local/sbin/oveo-backup-alert
install -m 0755 "$root/deploy/backup_tool.py" /usr/local/lib/oveo/backup_tool.py
install -m 0755 "$root/deploy/validate_staging.py" /usr/local/lib/oveo/validate_staging.py
install -m 0644 "$root/deploy/runtime.env.example" /etc/oveo/runtime.env.example
install -m 0644 "$root/deploy/backup.env.example" /etc/oveo/backup.env.example
install -m 0644 "$root/REHEARSAL" /opt/oveo/rehearsal.env
install -m 0644 "$root/deploy/systemd/oveo-backup.service" \
  /etc/systemd/system/oveo-backup.service
install -m 0644 "$root/deploy/systemd/oveo-backup-failure.service" \
  /etc/systemd/system/oveo-backup-failure.service
install -m 0644 "$root/deploy/systemd/oveo-backup.timer" \
  /etc/systemd/system/oveo-backup.timer
systemctl daemon-reload

echo "Installed Oveo host artifacts without changing runtime secrets, data, or timer state."
