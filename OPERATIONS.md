# Oveo operations

Oveo runs as one container bound to `127.0.0.1:8000` behind the existing Caddy service. Images are built in GitHub Actions and production always records an immutable `ghcr.io/pstngh/oveo@sha256:...` reference. Never build application dependencies on the VPS.

## Verified production host

The production target is `87.106.103.162` (`my-vps`, Debian 13, amd64, one vCPU). At the pre-cutover inventory it had 854 MiB RAM, 2 GiB swap, and 2.3 GiB free on a 9.7 GiB root filesystem. Root and `debian` key authentication were verified with the locally configured Ionos key. Caddy owns public ports 80/443 and a trusted short-lived Let's Encrypt IP certificate; preserve `/etc/caddy/Caddyfile` and `/var/lib/caddy`. The host has no UFW/firewalld INPUT policy, so loopback binding is mandatory.

Unrelated resources must never be touched: the `noku-bot` Compose project, its images, network, volumes, `/opt/noku-bot`, `/opt/noku-bot-backups`, `fortebot.service`, `/home/debian/fortebot`, and all shared Caddy, Docker, containerd, SSH, and system-timer state.

## One-time host bootstrap

Install Docker Compose, Python 3, curl, util-linux (`flock`), and `age` from Debian packages. Do not install Node or build Python dependencies on the host.

Copy the deployment artifacts with root ownership:

```text
/opt/oveo/compose.yml                         <- compose.yml
/usr/local/sbin/oveo-deploy                  <- deploy/oveo-deploy.sh
/usr/local/sbin/oveo-backup                  <- deploy/oveo-backup.sh
/usr/local/sbin/oveo-restore                 <- deploy/oveo-restore.sh
/usr/local/lib/oveo/backup_tool.py            <- deploy/backup_tool.py
/usr/local/lib/oveo/validate_staging.py       <- deploy/validate_staging.py
/opt/oveo/rehearsal.env                       <- exact-image CI rehearsal attestation
/etc/systemd/system/oveo-backup.service       <- deploy/systemd/oveo-backup.service
/etc/systemd/system/oveo-backup.timer         <- deploy/systemd/oveo-backup.timer
```

The checksummed `oveo-host-COMMIT.tar.gz` workflow artifact packages all of these files plus `deploy/install-host.sh`; after verifying its adjacent SHA-256 file, extract it and run that installer as root. It installs executable and configuration artifacts with the required modes, creates the host directories, and reloads systemd without enabling the timer or changing secrets. `/etc/oveo` and `/var/backups/oveo` are mode `0700`; `/var/lib/oveo` is mode `0700` owned by UID/GID 10001.

Create `/etc/oveo/runtime.env` from `deploy/runtime.env.example`, root-owned mode `0600`. Replace every example value: the OpenRouter key must be the working `sk-or-v1-...` credential and both password values must be complete Argon2id encodings, mapped OWNER to `charles` and USER to `yousra`. Stage only those three existing credentials; do not copy v1 sessions, encryption keys, or content. Single-quote Argon2 strings so their `$` characters remain literal. Validate the file without printing values with `python3 /usr/local/lib/oveo/validate_staging.py --runtime /etc/oveo/runtime.env`. Both deployment and v1 removal repeat this check. The OpenRouter request layer must retain model `openai/gpt-5.6-luna`, preferred provider `azure/eu`, same-model fallback, `data_collection=deny`, and `zdr=true`.

Create new backup encryption material; the v1 host has none to preserve:

```bash
install -d -m 0700 /etc/oveo /var/backups/oveo
age-keygen -o /etc/oveo/backup.agekey
chmod 0400 /etc/oveo/backup.agekey
age-keygen -y /etc/oveo/backup.agekey
```

Put only that last public recipient and `AGE_IDENTITY_FILE=/etc/oveo/backup.agekey` in `/etc/oveo/backup.env`, root-owned mode `0600`. The identity remains on this VPS by design; total VPS loss is not covered.

Configure these GitHub Actions secrets without printing them: `VPS_HOST=87.106.103.162`, `VPS_USER=root`, `VPS_SSH_PRIVATE_KEY`, and `VPS_KNOWN_HOSTS`. The pinned known-host value must come from the already verified local entry, not a fresh unauthenticated key scan.

## Initial cutover

Leave the repository variable `OVEO_DEPLOY_ENABLED` absent or set to `false` for the first push. That push must pass CI and always publishes the immutable image and checksummed host bundle, but the deploy job is skipped. Before creating that bundle, CI pulls the published digest, validates its revision label and Compose configuration, runs the exact image with a fresh empty data directory under the production limits, executes its migrations, seeds two synthetic Argon2 accounts, and waits for readiness. Only a successful rehearsal creates the bundled `REHEARSAL` attestation. Download the bundle, verify its SHA-256 file, confirm that `IMAGE` equals the `OVEO_REHEARSED_IMAGE` in `REHEARSAL`, run `deploy/install-host.sh`, and re-run the host inventory immediately before cutover. Stage `/etc/oveo/runtime.env`, the new age identity/configuration, and all other prerequisites first. Then, while authenticated to GHCR with an isolated temporary Docker configuration, run:

```bash
/usr/local/sbin/oveo-remove-v1 ghcr.io/pstngh/oveo@sha256:DIGEST --confirm REMOVE_V1
/usr/local/sbin/oveo-deploy ghcr.io/pstngh/oveo@sha256:DIGEST
```

`remove-v1.sh` refuses to proceed unless the candidate digest exactly matches the installed successful-rehearsal attestation and the exact old Compose labels, mount, volume, network, images, paths, Caddy route, non-placeholder staged credentials, and backup encryption prerequisites match. Its authorized targets are only `translation-agent-staging-app-1`, `translation-agent-staging_translation-data`, `translation-agent-staging_default`, the `translation-agent:*` images carrying source-revision labels, `/opt/translation-agent-staging`, `/etc/translation-agent`, and `/run/translation-agent`. It never touches Caddy or unrelated services and never invokes Docker system, volume, image, or builder pruning.

The old BuildKit cache cannot be safely selected by Compose project. Do not broad-prune it. The host's pre-existing seven-day unused-cache timer will eventually reclaim it; re-inventory before changing that global policy.

After the manual cutover, public health, real-provider smoke test, account checks, deletion/cost check, and initial backup/restore all pass, set the repository variable with `gh variable set OVEO_DEPLOY_ENABLED --body true --repo pstngh/oveo`. Dispatch `ci-deploy.yml` on `main` once. This reruns the same commit, publishes an exact digest, installs its checksummed host bundle, and deploys it. From then on, every passing push to `main` follows the same automatic path. If the variable is unset or anything other than lowercase `true`, CI still tests and publishes but cannot deploy.

## Deployment and rollback

Every passing push to `main` builds off-host, publishes a SHA-tagged image, resolves its immutable digest, and calls `oveo-deploy`. The host script serializes maintenance with `flock`, validates the exact repository/digest, pulls it, starts the service, and requires both loopback and public HTTPS readiness. If startup or health fails, it restores the preceding digest, pulling it from GHCR when necessary. After success it removes only superseded digest references from `ghcr.io/pstngh/oveo`; there is no global prune.

CI gives the deployment job read-only package permission. Its short-lived GHCR login uses a private temporary `DOCKER_CONFIG` inside the staged bundle and is removed afterward, so it never reads, overwrites, or logs out any pre-existing root Docker credentials on the shared VPS.

Manual status and health checks:

```bash
docker compose --project-name oveo --env-file /etc/oveo/deploy.env -f /opt/oveo/compose.yml ps
curl --fail http://127.0.0.1:8000/health/ready
curl --fail https://87.106.103.162/health/ready
cat /var/lib/oveo/deployed-image
```

## Password reset

Reset either permanent account interactively from inside the running application container so the plaintext password never enters shell history:

```bash
docker compose --project-name oveo --env-file /etc/oveo/deploy.env \
  -f /opt/oveo/compose.yml exec app oveo-admin reset-password charles
# Or replace charles with yousra.
```

The command writes a new Argon2id hash, increments the account's credential version, and revokes all of that account's sessions. The hashes in `/etc/oveo/runtime.env` seed missing accounts only; they do not overwrite a password changed by this command on restart. The reset database state is included in the next encrypted backup.

## Backups and restore

Enable the nightly timer only after an initial backup and disposable restore both pass:

```bash
/usr/local/sbin/oveo-backup
latest=$(find /var/backups/oveo -maxdepth 1 -type f -name 'oveo-*.tar.gz.age' | sort | tail -n 1)
/usr/local/sbin/oveo-restore "$latest" --destination /var/tmp/oveo-restore-test
rm -rf -- /var/tmp/oveo-restore-test
systemctl daemon-reload
systemctl enable --now oveo-backup.timer
systemctl list-timers oveo-backup.timer
```

The backup helper holds a SQLite writer lock, uses SQLite's online backup API, copies only regular attachment files, rejects any file not referenced by the snapshot, validates attachment sizes and SHA-256 values, runs SQLite quick/foreign-key checks, encrypts with `age`, decrypts and restores into a disposable directory for verification, and only then publishes the backup. The seven newest successful encrypted backups are retained. Deleted content can therefore remain for at most seven successful daily rotations.

Test a restore at any time with `--destination`. A live restore is intentionally explicit and keeps both the pre-restore tree and any rejected restored tree:

```bash
/usr/local/sbin/oveo-restore /var/backups/oveo/oveo-TIMESTAMP.tar.gz.age --live --confirm RESTORE
```

After successful application-level verification, remove only the exact timestamped `/var/lib/oveo.pre-restore.TIMESTAMP` directory. Never glob or prune `/var/lib`.

## Incidents

Inspect container state and content-free logs with `docker compose ... ps` and `docker compose ... logs --since 30m app`. Do not log or paste prompts, messages, attachment contents, cookies, provider bodies, or secrets. Disk recovery must target only exact superseded Oveo digest references and documented timestamped restore directories. Never run `docker system prune`, `docker image prune`, `docker volume prune`, or a global builder prune on this shared VPS.
