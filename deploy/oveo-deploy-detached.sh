#!/bin/sh
# The deploy job's entry point on the host, run from the verified, extracted bundle
# with the registry token on stdin. oveo-deploy runs as its own systemd unit, so a
# cancelled job or a dropped SSH connection cannot stop it halfway (for example after
# it closed writes and stopped Oveo). Its output goes to the journal, never into the
# SSH session, and is followed from there; the unit removes the staged bundle and the
# registry login when it ends.
set -eu
umask 077

die() {
  echo "oveo-deploy-detached: $*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "must run as root"
stage=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
case "$stage" in
  /var/tmp/oveo-host-*) ;;
  *) die "the bundle must be staged as /var/tmp/oveo-host-COMMIT" ;;
esac
archive=$stage.tar.gz
checksum=$archive.sha256
docker_config=$stage/docker-config
unit=oveo-deploy-${stage#/var/tmp/oveo-host-}

cleanup() {
  if [ -d "$docker_config" ]; then
    DOCKER_CONFIG=$docker_config docker logout ghcr.io >/dev/null 2>&1 || true
  fi
  rm -f -- "$archive" "$checksum"
  rm -rf -- "$stage"
}

# Inside the unit: deploy, then remove the bundle whatever the outcome.
if [ "$#" -eq 2 ] && [ "$1" = --in-unit ]; then
  trap cleanup EXIT
  "$stage/deploy/oveo-deploy.sh" --bundle "$stage" "$2"
  exit 0
fi

[ "$#" -eq 2 ] || die "usage: $0 ghcr.io/pstngh/oveo@sha256:DIGEST GHCR_USER"
image=$1
user=$2

# Until the unit runs, the bundle and login are removed here; once it runs, it owns them.
unit_running() {
  case "$(systemctl show --property=ActiveState --value "$unit.service" 2>/dev/null)" in
    "" | inactive | failed) return 1 ;;
    *) return 0 ;;
  esac
}
follower=
finish() {
  [ -z "$follower" ] || kill "$follower" 2>/dev/null || true
  unit_running || cleanup
}
trap finish EXIT
trap 'exit 1' HUP INT TERM

install -d -m 0700 "$docker_config"
IFS= read -r token || die "no registry token on stdin"
printf '%s' "$token" | DOCKER_CONFIG=$docker_config \
  docker login ghcr.io --username "$user" --password-stdin >/dev/null \
  || die "could not sign in to ghcr.io"
unset token

since=$(date '+%Y-%m-%d %H:%M:%S')
journalctl --follow --quiet --output=cat --since="$since" --unit="$unit.service" &
follower=$!
status=0
systemd-run --unit="$unit" --collect --quiet --wait --service-type=exec \
  --setenv=DOCKER_CONFIG="$docker_config" \
  "$stage/deploy/oveo-deploy-detached.sh" --in-unit "$image" || status=$?
# Give the journal a moment to deliver the unit's last lines.
sleep 2
exit "$status"
