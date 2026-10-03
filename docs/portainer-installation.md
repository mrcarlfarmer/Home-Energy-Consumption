# Raspberry Pi installation with Portainer

## Deployment choices

This plan uses **Portainer with a Docker Standalone environment on a 64-bit Raspberry Pi**, one application container, native HTTPS and persistent local storage. It does not deploy anything automatically. If the environment is Docker Swarm, do not use this stack unchanged.

The recommended route is to build the ARM64 image on the Pi, then paste [the Portainer stack](../deploy/portainer-stack.yml) into Portainer's Web editor. No image registry or paid relative-path feature is required. The stack deliberately has no `build:` context and uses `pull_policy: never`: the selected image must already exist on the **Pi's Docker endpoint**, not just on your Windows PC or the Portainer server.

Build from **`main`**, which contains the application, polling controls, short chart ranges, persistent login sessions and this installation configuration. Tag the image with the actual source commit so the installed version is identifiable. This is the initial native-Pi acceptance deployment, not a claim that Pi performance has already been measured.

### Values to choose

Every address below is an example. Replace it consistently before running commands.

| Setting | Example | Purpose |
|---|---|---|
| Pi SSH user | `YOUR_PI_SSH_USER` | Your existing SSH account; not necessarily `pi` |
| `PI_LAN_IP` | `192.168.1.50` | Reserve this IPv4 address in DHCP |
| `ENERGY_HOSTNAME` | `energy.home.arpa` | Local DNS name pointing to the Pi |
| `HTTPS_PORT` | `8443` | Published app port; change if already occupied |
| `ENERGY_ROOT` | `/opt/home-energy` | Local storage on the Pi, preferably SSD-backed |
| `ENERGY_IMAGE` | `home-energy-monitor:pi-<commit>` | Use the exact tag printed by the build below |
| Portainer stack name | `home-energy` | Used by the operational examples |

The resulting address is **`https://energy.home.arpa:8443`**. The IP address also works if it is included in the certificate. Portainer's own HTTPS certificate does not automatically cover this separate application.

## 1. Check the Pi

The following shell commands run **over SSH on the Pi**, not in Windows PowerShell or inside the Portainer container:

```sh
uname -m
getconf LONG_BIT
docker version
docker buildx version
docker info --format '{{.Architecture}} {{.DockerRootDir}}'
free -h
df -h /opt
timedatectl status
```

Require `aarch64`/`arm64`, a 64-bit userspace, a working Docker engine and synchronized time. A 32-bit Raspberry Pi OS installation needs upgrading to a 64-bit OS first. Use a supported Portainer release with Compose support on its Standalone endpoint.

The runtime limit is 140 MiB, but **image building, Docker, Portainer and the OS need additional RAM and disk**. Allow several GB of build space. If the Pi has little free RAM, use the alternative image-transfer route below rather than assuming the build fits the runtime limit.

Install Git and the Buildx plugin if missing, using your existing Docker installation's package source. For Docker's official Debian/Raspberry Pi OS repository, the plugin package is `docker-buildx-plugin`; do not mix Docker CE and distro Docker packages blindly. A Compose CLI on the Pi is optional for this Portainer workflow.

Set up the DHCP reservation and local DNS entry. Without local DNS, use the example IP URL after replacing it with your real Pi IP; keep the chosen hostname in the certificate for the container health probe. Check the published port is free with `sudo ss -ltnp`.

Do not forward the application port on your router. Bind only to the Pi's LAN address and use Docker-aware firewall rules or network ACLs if needed; do not assume a host UFW rule alone filters Docker-published ports.

## 2. Build the image on the Pi

Run as a user allowed to use Docker, or prefix Docker commands with `sudo`. Docker access is effectively root-equivalent.

```sh
git clone --branch main --single-branch https://github.com/mrcarlfarmer/Home-Energy-Consumption.git "$HOME/home-energy-source"
cd "$HOME/home-energy-source"
APP_REV=$(git rev-parse --short=12 HEAD)
ENERGY_IMAGE="home-energy-monitor:pi-${APP_REV}"
docker buildx build --platform linux/arm64 --load \
  --tag "$ENERGY_IMAGE" .
docker image inspect "$ENERGY_IMAGE" \
  --format '{{.Os}}/{{.Architecture}} {{.Id}}'
printf 'ENERGY_IMAGE=%s\n' "$ENERGY_IMAGE"
```

The inspection must report `linux/arm64`. Record the image ID and printed `ENERGY_IMAGE` value; use that exact value in Portainer. In any new Pi SSH session, set `ENERGY_IMAGE` to this tag again before running commands that reference it. The build requires outbound access to GitHub, Docker Hub, PyPI and npm; the running dashboard bundles its frontend and only needs the Kraken API, DNS and correct host time.

Use `deploy/portainer-stack.yml` from this checkout, not the development `docker-compose.yml`. If the source directory already exists, inspect any local changes before updating it; use `git pull --ff-only origin main` from a clean `main` checkout rather than overwriting a previous installation.

### Alternative: build elsewhere and transfer an ARM64 image

Use a known ARM64-capable builder, native or emulated. The Windows `home-energy-monitor:local` development image is AMD64 and must not be substituted. An ARM64 image built before the session/chart updates is also not the intended version.

After building the desired `main` commit with `--platform linux/arm64 --load` and a commit-specific tag, set `$EnergyImage` to that exact tag:

```powershell
# On the build PC; replace the destination SSH user and address.
$EnergyImage = "home-energy-monitor:pi-REPLACE_WITH_BUILD_COMMIT"
docker image inspect $EnergyImage --format '{{.Os}}/{{.Architecture}}'
docker save --output .\home-energy-arm64.tar $EnergyImage
scp .\home-energy-arm64.tar YOUR_PI_SSH_USER@192.168.1.50:home-energy-arm64.tar
```

On the Pi, run `docker load --input "$HOME/home-energy-arm64.tar"` and repeat the image inspection. Remove the transfer archive after a successful import if disk space is limited. This exports an application image, not your database or secrets.

## 3. Prepare storage and a trusted certificate

On the Pi, replace `/opt/home-energy` throughout if you chose another root:

```sh
sudo install -d -m 755 /opt/home-energy
sudo install -d -m 700 -o 10001 -g 10001 \
  /opt/home-energy/data /opt/home-energy/secrets
install -d -m 700 "$HOME/energy-install"
```

Do not use NFS/SMB for SQLite WAL. If using an SSD mount, ensure it is mounted before Docker starts. All bind paths in the stack refer to the **Pi**, even if Portainer itself runs elsewhere. Missing paths deliberately fail rather than creating empty, root-owned directories.

Obtain a leaf certificate and key from your trusted local CA. The certificate must include both `energy.home.arpa` and the actual Pi IP in its SANs. Do not reuse the Windows test certificate: it only covers localhost.

For a personal LAN, an explicit, manually maintained option is mkcert on your administration PC. It is a development/local-trust tool, not a public-production certificate service. Prefer your existing managed CA if available. Installing its CA changes that PC's trust store, so do this deliberately:

1. Install mkcert using its [official installation instructions](https://github.com/FiloSottile/mkcert#installation) or official release binary; reopen your terminal if needed.
2. From the existing Windows project folder, generate new Pi-specific files in a private, Git-ignored directory:

```powershell
$PiHost = "192.168.1.50"
$PiUser = "YOUR_PI_SSH_USER"
$SshTarget = "${PiUser}@${PiHost}"
New-Item -ItemType Directory -Force .\secrets\pi | Out-Null
mkcert -install
mkcert -cert-file .\secrets\pi\energy.pem -key-file .\secrets\pi\energy-key.pem energy.home.arpa $PiHost
ssh $SshTarget 'install -d -m 700 "$HOME/energy-install"'
scp .\secrets\pi\energy.pem .\secrets\pi\energy-key.pem "${SshTarget}:energy-install/"
```

Verify the Pi's SSH host key; do not disable SSH host verification. Install **only the public `rootCA.pem`** from `mkcert -CAROOT` on other browser devices that need access, following their browser/OS trust instructions. **Never copy `rootCA-key.pem` to the Pi, Portainer, this repository or other clients.** Protect the CA private key on the administration PC.

On the Pi:

```sh
sudo install -m 644 "$HOME/energy-install/energy.pem" /opt/home-energy/secrets/energy.pem
sudo install -o 10001 -g 10001 -m 400 \
  "$HOME/energy-install/energy-key.pem" /opt/home-energy/secrets/energy-key.pem
sudo openssl x509 -in /opt/home-energy/secrets/energy.pem -noout -dates -ext subjectAltName
```

Keep a certificate-renewal reminder. Replace the leaf certificate/key before expiry and recreate the app container so file mounts and the TLS context are refreshed. This stack does not perform automatic certificate renewal.

## 4. Create the bootstrap password file

On the Pi, choose a long unique password of at least 12 characters. It is not a Portainer or Octopus password.

```sh
IFS= read -r -s -p "Dashboard password (12+ characters): " PASSWORD
printf '\n'
printf '%s\n' "$PASSWORD" | sudo tee /opt/home-energy/secrets/admin-password >/dev/null
unset PASSWORD
sudo chown 10001:10001 /opt/home-energy/secrets/admin-password
sudo chmod 400 /opt/home-energy/secrets/admin-password
```

The image runs as UID/GID `10001:10001`; file ownership matters. Do not make secrets world-readable to fix permissions. For a new database, this file creates the administrator hash. For an imported database, it does **not** overwrite the existing hash; follow the reset step below.

## 5. Choose a fresh installation or migrate the current data

**Fresh installation:** leave `/opt/home-energy/data` empty. The app initializes it on first start; enter your Octopus details in its UI afterward. Do not run the PC collector and Pi collector simultaneously for the same meter.

**Recommended for the existing trial: migrate its settings and history.** SQLite is portable between AMD64 and ARM64. Use its backup API, not a copy of the active `.sqlite3` file without WAL.

From the existing project folder in **Windows PowerShell**, after the Pi image/certificates are ready:

```powershell
$PiHost = "192.168.1.50"
$PiUser = "YOUR_PI_SSH_USER"
$SshTarget = "${PiUser}@${PiHost}"
$BackupName = "pi-transfer-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".sqlite3"
docker compose stop energy
if ($LASTEXITCODE -ne 0) { throw "Could not stop the PC collector" }
docker compose run --rm --no-deps energy energy-admin backup --output "/data/backups/$BackupName"
if ($LASTEXITCODE -ne 0) { throw "Backup failed; do not proceed with migration" }
docker compose cp "energy:/data/backups/$BackupName" ".\secrets\$BackupName"
if ($LASTEXITCODE -ne 0) { throw "Could not export the backup" }
scp ".\secrets\$BackupName" "${SshTarget}:energy-install/energy.sqlite3"
if ($LASTEXITCODE -ne 0) { throw "Backup transfer failed" }
```

Leave the PC collector stopped after a successful cutover. If the transfer/setup fails before the Pi is activated, you can resume the PC with `docker compose start energy`; stop it again before starting the Pi. Do not use `docker compose down -v`: retain the original volume as a recovery copy. The backup contains your Octopus API key, configuration, password hash, session metadata and history; keep it private.

On the Pi, **before deploying the stack**, install into the new empty data directory:

```sh
if [ -n "$(sudo find /opt/home-energy/data -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "The target data directory is not empty. Stop and choose a recovery plan; do not overwrite it."
else
  sudo install -o 10001 -g 10001 -m 600 \
    "$HOME/energy-install/energy.sqlite3" /opt/home-energy/data/energy.sqlite3
fi
```

If the target was not empty, do not continue with the commands below until you have resolved that conflict. This also guards against stale WAL/SHM files. Never replace a database under a running container.

Revoke sessions copied in the backup and set the Pi dashboard password to the one chosen in step 4:

```sh
docker run --rm -it --network none --user 10001:10001 \
  --memory 140m --memory-swap 140m \
  --mount type=bind,src=/opt/home-energy/data,dst=/data \
  --entrypoint energy-admin "${ENERGY_IMAGE:?Set ENERGY_IMAGE to your built image tag}" password-reset
```

This retains the Octopus key, selected meter, polling configuration and readings. Minimize the cutover gap: recovery is limited to recent data, not arbitrary historical backfill. Clean up the specifically named staging copies of the private key/database after the Pi is accepted and a protected backup is retained.

## 6. Deploy through Portainer

1. Select the Pi's **Docker Standalone environment**. Confirm the versioned image appears under its **Images** view.
2. Open **Stacks > Add stack**, name it **`home-energy`**, and choose **Web editor**.
3. Paste the complete contents of [deploy/portainer-stack.yml](../deploy/portainer-stack.yml).
4. Add the following variables in Portainer's **Environment variables** section, or save this block as a local `.env` file and use **Load variables from .env file**. Replace the example values:

```dotenv
ENERGY_IMAGE=home-energy-monitor:pi-REPLACE_WITH_BUILD_COMMIT
ENERGY_ROOT=/opt/home-energy
PI_LAN_IP=192.168.1.50
ENERGY_HOSTNAME=energy.home.arpa
HTTPS_PORT=8443
```

5. Leave image re-pulling and automatic Git/webhook updates disabled. This plan uses a locally built image, not a registry-published image.
6. Click **Deploy the stack** and inspect the container logs and health status.

The YAML derives trusted Host/Origin values from the hostname, IP and port, so they remain consistent. It mounts the password and certificate files read-only rather than depending on Swarm secrets or relative paths. **Do not put your Octopus API key, dashboard password or CA private key into the stack YAML or Portainer variables.** Polling interval, account/meter selection and timezone are application settings, not invented container environment variables.

The container retains the development deployment's hardening: non-root user, read-only root filesystem, private writable `/data`, 8 MiB `/tmp`, dropped capabilities, no privilege escalation, bounded logs and a 140 MiB memory limit with no additional swap allowance.

## 7. First login and acceptance

Open `https://energy.home.arpa:8443` (or your actual IP/port) directly in a browser. The certificate should be trusted with a matching hostname; do not treat clicking through a warning as completing the HTTPS setup.

For a fresh database, sign in using the bootstrap password, enter your Octopus API key/account, discover and select the meter, save settings, then click **Start polling**. For an imported database, use the password from the reset step; saved polling may already be enabled.

Confirm all of the following before retiring the PC instance:

- Container health becomes healthy and live demand has a recent native reading timestamp.
- Account/meter settings and any imported history are present; no credential values appear in logs.
- The short chart ranges work, and coverage/15-minute averages fill in as sufficient supported data arrives.
- A normal container restart preserves the new Pi browser session and resumes collection. Sessions still expire 12 hours after sign-in; changing hostnames requires signing in on the new hostname.
- Memory and swap limits are actually applied, and the host reports no OOM/restart loop.

On the Pi, inspect the deployed container:

```sh
CONTAINER=$(docker ps --filter label=com.docker.compose.project=home-energy \
  --filter label=com.docker.compose.service=energy --format '{{.ID}}')
docker inspect "$CONTAINER" --format '{{.State.Health.Status}}'
docker inspect "$CONTAINER" \
  --format 'Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}} User={{.Config.User}} ReadOnly={{.HostConfig.ReadonlyRootfs}}'
docker stats --no-stream "$CONTAINER"
```

Expected limits are **146800640** bytes for both Memory and MemorySwap, user **10001:10001**, and ReadOnly **true**. `docker stats` is a quick check, not a substitute for full cgroup-accounted measurements. The Pi/OS, Docker and Portainer consume additional memory. The native-Pi workload in [validation](validation.md) remains the longer-term acceptance test.

## 8. Backups, upgrades and recovery

Keep timestamped backups on separate protected storage, not only alongside the live database. For a quiesced backup, stop the app in Portainer, then run on the Pi:

```sh
docker run --rm --network none --user 10001:10001 \
  --memory 140m --memory-swap 140m \
  --mount type=bind,src=/opt/home-energy/data,dst=/data \
  --entrypoint energy-admin "${ENERGY_IMAGE:?Set ENERGY_IMAGE to your deployed image tag}" \
  backup --output "/data/backups/energy-$(date +%Y%m%d-%H%M%S).sqlite3"
```

Restart the existing container in Portainer afterward. The command refuses to overwrite a backup. For recovery, restore only while stopped and revoke restored sessions with `password-reset` before serving the application again.

For an upgrade, build a **new versioned ARM64 image**, take a backup, change only `ENERGY_IMAGE` in Portainer and update/recreate the stack without re-pulling. Preserve the data/secret mounts. Keep the prior image and its compatible backup until the upgrade is accepted; older images refuse newer database schemas, so rollback can require restoring the matching backup rather than simply choosing the old image.

Do not delete the bind-mounted data directory when removing/recreating a stack. See [operations](operations.md) for recovery and storage details.

### Common installation failures

| Symptom | Check |
|---|---|
| Image not found / pull access denied | Build or load the exact tag on the Pi endpoint; do not enable image re-pulling |
| `exec format error` | Image and host must both be ARM64; do not load the Windows AMD64 image |
| Bind source missing | Paths/files must exist on the Pi, not the Portainer server; check `ENERGY_ROOT` |
| Permission denied / SQLite cannot open | Data directory and private files must be accessible to UID 10001 |
| Unhealthy TLS probe | First configured hostname must be in the certificate SANs; check time, expiry and certificate/key pairing |
| Browser TLS warning | Trust the correct public CA certificate and use a SAN-covered hostname/IP |
| Host rejected / mutation returns 403 | Check `ENERGY_HOSTNAME`, `PI_LAN_IP` and `HTTPS_PORT`; don't use an unconfigured alias or proxy URL |
| No live data after successful connectivity test | Save the selected meter and explicitly start polling; inspect collection/freshness status |

## References

- [Portainer: adding a Docker stack, Web editor and variables](https://docs.portainer.io/user/docker/stacks/add)
- [Docker: bind mounts are on the daemon host](https://docs.docker.com/engine/storage/bind-mounts/)
- [Docker Compose service configuration and pull policy](https://docs.docker.com/reference/compose-file/services/#pull_policy)
- [mkcert: local trust, installation and CA-key precautions](https://github.com/FiloSottile/mkcert)
