#!/bin/sh
# Run by oveo-backup-failure.service (OnFailure= of oveo-backup.service). It sends
# nothing off the host: it leaves a record next to the backups and a critical journal
# entry, which the next successful backup clears (see OPERATIONS.md).
set -eu
umask 077

record=/var/backups/oveo/BACKUP-FAILED
stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

printf 'The Oveo backup failed at %s. See: journalctl -u oveo-backup.service\n' "$stamp" \
  >"$record"
logger -p daemon.crit -t oveo-backup \
  "The Oveo backup failed at $stamp; see journalctl -u oveo-backup.service"
