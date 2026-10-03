# Certificate-free Portainer installation (trusted LAN only)

Use [deploy/portainer-http-stack.yml](../deploy/portainer-http-stack.yml) for **HTTP without any certificate files**. Keep the dashboard password. The application still authenticates users, checks CSRF and trusted Origins/Hosts, and retains sessions across restarts.

**Tradeoff:** HTTP does not encrypt traffic between your browser and the Pi. Passwords, session cookies, telemetry and the Octopus API key when entered can be observed or modified by someone able to intercept that connection. A password does not replace transport encryption. Use this only on a trusted LAN with no router port forwarding or public/untrusted-network exposure. The Pi's connection to Octopus still uses verified HTTPS.

HTTPS remains the application's default, and the existing HTTPS stack is unchanged. This separate stack explicitly sets `APP_TRANSPORT=http`; omitting that setting does not silently disable TLS. "Trusted LAN" describes the deployment boundary, not an automatic guarantee made by the application.

## 1. Build the updated image on the Pi

This assumes 64-bit Raspberry Pi OS/Debian and Portainer managing a Docker Standalone endpoint. Git and Docker Buildx are required. If you have not cloned the repository yet:

```sh
git clone --branch main --single-branch \
  https://github.com/mrcarlfarmer/Home-Energy-Consumption.git "$HOME/home-energy-source"
```

For either a new or existing clean `main` checkout:

```sh
cd "$HOME/home-energy-source" &&
if [ "$(git branch --show-current)" != main ] || [ -n "$(git status --porcelain)" ]; then
  echo "Stop: use a clean main checkout before updating and building."
else
  git pull --ff-only origin main &&
  APP_REV=$(git rev-parse --short=12 HEAD) &&
  ENERGY_IMAGE="home-energy-monitor:pi-${APP_REV}" &&
  docker buildx build --platform linux/arm64 --load --tag "$ENERGY_IMAGE" . &&
  docker image inspect "$ENERGY_IMAGE" --format '{{.Os}}/{{.Architecture}} {{.Id}}' &&
  printf 'ENERGY_IMAGE=%s\n' "$ENERGY_IMAGE"
fi
```

If Git reports local changes or a non-fast-forward update, resolve that before building; do not overwrite your work. Expect `linux/arm64` from the inspection. Copy the printed image tag for Portainer. An older image that only supported HTTPS will not work with this stack: pull and rebuild first.

The image must exist on the Pi's Docker endpoint. If building elsewhere, use an ARM64-capable builder and the [image transfer procedure](portainer-installation.md#alternative-build-elsewhere-and-transfer-an-arm64-image); do not copy the Windows AMD64 development image. Build-time RAM and disk needs exceed the 140 MiB runtime limit.

## 2. Prepare storage and the password

Run on the Pi over SSH:

```sh
sudo install -d -m 755 /opt/home-energy
sudo install -d -m 700 -o 10001 -g 10001 \
  /opt/home-energy/data /opt/home-energy/secrets
```

For a new installation, create a long unique password of at least 12 characters:

```sh
IFS= read -r -s -p "Dashboard password (12+ characters): " PASSWORD
printf '\n'
printf '%s\n' "$PASSWORD" | sudo tee /opt/home-energy/secrets/admin-password >/dev/null
unset PASSWORD
sudo chown 10001:10001 /opt/home-energy/secrets/admin-password
sudo chmod 400 /opt/home-energy/secrets/admin-password
```

If the password file/data already exist, retain them rather than overwriting them. The bootstrap file does not reset an existing database password. The container uses UID/GID `10001:10001`; do not solve permission issues by making secrets world-readable.

Use local storage, preferably an SSD, not NFS/SMB for SQLite WAL. Ensure any storage mount is available before Docker starts. These paths must exist on the Pi's Docker endpoint, not just on the Portainer server.

**No mkcert, CA installation, certificate or TLS private key is needed.** If HTTPS certificate files already exist, you may leave them in place; this stack does not mount or read them.

For a fresh installation, leave the data directory empty. To keep your current PC settings/history, first run `install -d -m 700 "$HOME/energy-install"` on the Pi to create the private transfer staging directory. Then follow [the data migration procedure](portainer-installation.md#5-choose-a-fresh-installation-or-migrate-the-current-data), skipping all certificate steps. Transfer only into a stopped, empty destination and reset the imported dashboard password to revoke copied sessions. Do not run both collectors against the same meter simultaneously.

## 3. Choose the hostname and an available host port

Configure your LAN DNS to resolve **`home.energy` to `192.168.1.2`**. Verify that from the browser device with `nslookup home.energy`. The `.energy` suffix is public, so this name needs the correct local resolution rather than an assumption about public DNS. You can also use the IP directly.

Other containers can keep their existing ports on the same IP. Check for an available port on the Pi:

```sh
docker ps --format 'table {{.Names}}\t{{.Ports}}'
sudo ss -ltnp
```

The HTTP stack defaults to **host port 8080**, giving:

```text
http://home.energy:8080
http://192.168.1.2:8080
```

If 8080 is occupied, choose a free `HTTP_PORT`, for example 8081. DNS does not choose a port; include the selected port in the URL. If host port 80 is free, `HTTP_PORT=80` permits `http://home.energy` without a port suffix. The application normalizes default-port Origins correctly.

The container always listens on internal port **8443**, now speaking HTTP in this explicit mode. The host-to-container mapping is `192.168.1.2:8080 -> container:8443`; the internal port number does not force TLS. Do not enable Docker host-network mode.

Use `home.energy` specifically for this app. Cookies are host-scoped, not isolated by port, so unrelated services should keep their own hostnames/IP URLs.

## 4. Create or update the Portainer stack

1. Select the Pi's Docker Standalone environment.
2. Choose **Stacks > Add stack**, name it `home-energy`, and use **Web editor**. If this stack already exists, back up its database and edit that same stack rather than starting a second collector.
3. Paste [deploy/portainer-http-stack.yml](../deploy/portainer-http-stack.yml), not the HTTPS stack.
4. Set these variables, replacing the image placeholder with the tag printed by the build:

```dotenv
ENERGY_IMAGE=home-energy-monitor:pi-REPLACE_WITH_BUILD_COMMIT
ENERGY_ROOT=/opt/home-energy
PI_LAN_IP=192.168.1.2
ENERGY_HOSTNAME=home.energy
HTTP_PORT=8080
```

5. Leave image re-pulling disabled: `pull_policy: never` requires the locally built image.
6. Deploy/update the stack and confirm the container becomes healthy.

There are only two host mounts: the private data directory and the read-only password file. There are no certificate mounts or TLS variables. Do not put your dashboard password or Octopus API key in the stack or its environment-variable form.

The stack retains the 140 MiB memory limit, no extra swap allowance, non-root user, read-only root filesystem, bounded logs and dropped capabilities. Keep capacity for the Pi OS, Docker, Portainer and other containers. Bind to the reserved LAN IP and use Docker-aware firewall/network restrictions; do not expose the published HTTP port on the internet.

## 5. Open the dashboard

Open **`http://home.energy:8080`**, not `https://`. Sign in, enter or retain your Octopus settings, select the meter and enable polling. Confirm recent readings arrive and your existing history is present.

HTTP mode keeps HttpOnly/SameSite session cookies but deliberately omits the Secure flag and HSTS so HTTP login works. Requests with an untrusted Host/Origin or missing CSRF token are still rejected. Session expiry remains 12 hours after sign-in.

If you previously used HTTPS at this hostname, the browser may remember HSTS and automatically upgrade HTTP requests to HTTPS. Remove only this application's remembered HSTS/site state using the browser's controls, or use a fresh dedicated hostname with matching DNS/Portainer settings. Clear old cookies for this app if an old Secure cookie prevents a new HTTP login. Do not disable browser security globally.

The health probe uses loopback HTTP in this mode; it does not read certificates or disable validation for any HTTPS connection.

## Backups and later changes

Use the [backup and upgrade procedures](portainer-installation.md#8-backups-upgrades-and-recovery) with the currently deployed image tag and unchanged data directory. Backups contain secrets and need private storage. Revoke sessions when restoring an old backup.

To return to HTTPS, obtain the certificate files and use the [HTTPS installation guide](portainer-installation.md) and original stack. Never simply change URL schemes without also changing the transport, trusted Origins, port mapping and mounts. A reverse-proxy deployment is a separate configuration; this HTTP mode does not automatically trust forwarded headers.
