# Username-protected, certificate-free Portainer installation (trusted LAN only)

Use [deploy/portainer-http-stack.yml](../deploy/portainer-http-stack.yml) for **HTTP without certificates, with username/password protection for the entire dashboard and API**. The default username is `admin`; a private password file is required. CSRF and trusted Origin/Host checks remain enabled, and saved API keys are redacted.

**Tradeoff:** login prevents unauthenticated access, but HTTP leaves usernames, passwords, session cookies, telemetry and API-key entry unencrypted. Intercepted traffic can be observed or modified. Use this only on a trusted LAN with no public exposure; HTTPS remains the secure transport alternative. The Pi's Kraken connection still uses verified HTTPS.

Both Portainer stacks now require authentication. The HTTP stack explicitly sets `APP_TRANSPORT=http` and `APP_AUTH_REQUIRED=true`. These are deployment settings, not dashboard switches.

## 1. Select a published image

This assumes 64-bit Raspberry Pi OS/Debian and Portainer managing a Docker Standalone endpoint. **You no longer need to build on the Pi.** GitHub Actions publishes verified ARM64/AMD64 images to GHCR automatically.

Follow [registry access and image selection](registry-deployment.md) to add GHCR to Portainer if the image is private. Copy the exact image digest from a successful `Verify` run's Summary for `ENERGY_IMAGE`. Check the package's actual visibility in GitHub rather than assuming it follows repository visibility; a classic read-only package PAT belongs in Portainer's registry credentials, not the stack.

For a local-build fallback, use the [native Pi build instructions](portainer-installation.md#2-build-the-image-on-the-pi) or [ARM64 image transfer](portainer-installation.md#alternative-build-elsewhere-and-transfer-an-arm64-image), then set `ENERGY_PULL_POLICY=never`. Older images from before password-free support still require login.

## 2. Prepare storage

Run on the Pi over SSH:

```sh
sudo install -d -m 755 /opt/home-energy
sudo install -d -m 700 -o 10001 -g 10001 /opt/home-energy/data /opt/home-energy/secrets
```

Retain existing data rather than overwriting it. The container uses UID/GID `10001:10001`; do not solve permission issues by making the private database world-readable.

Use local storage, preferably an SSD, not NFS/SMB for SQLite WAL. Ensure any storage mount is available before Docker starts. These paths must exist on the Pi's Docker endpoint, not just on the Portainer server.

No certificates are needed, but create a private password file before updating the stack. On the Pi in Bash:

```sh
IFS= read -r -s -p "Dashboard password (12+ characters): " PASSWORD
printf '\n'
printf '%s\n' "$PASSWORD" | sudo tee /opt/home-energy/secrets/admin-password >/dev/null
unset PASSWORD
sudo chown 10001:10001 /opt/home-energy/secrets/admin-password
sudo chmod 400 /opt/home-energy/secrets/admin-password
```

For a fresh/password-free database this bootstraps the hash. If an old administrator hash already exists, the file does not replace it: use your previous password or reset it while the old password-free container is still running with `docker exec -it home-energy energy-admin password-reset`. That reset preserves telemetry/configuration. Do not put passwords in Portainer environment variables or Git.

For a fresh installation leave the data directory empty. For migration follow [the database transfer procedure](portainer-installation.md#5-choose-a-fresh-installation-or-migrate-the-current-data), skipping certificates but retaining password reset to revoke copied sessions. Never overwrite existing data or run two collectors against it.

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
2. Choose **Stacks > Add stack**, name it `home-energy`, and select **Git repository**. Use repository URL `https://github.com/mrcarlfarmer/Home-Energy-Consumption.git`, reference `refs/heads/main`, Compose path `deploy/portainer-http-stack.yml`, and leave GitOps updates disabled. Newer Portainer versions may ask you to create/select a Git source first.
3. Follow the [Git-backed stack setup and existing-stack migration guidance](registry-deployment.md#deploy-the-stack-from-the-git-repository). If this stack already exists, back up its database and migrate it rather than starting a second collector. Web editor remains an alternative: paste [the HTTP YAML](../deploy/portainer-http-stack.yml), not the HTTPS stack.
4. Set these variables, replacing the digest placeholder with the reference from the successful publication:

```dotenv
ENERGY_IMAGE=ghcr.io/mrcarlfarmer/home-energy-consumption@sha256:REPLACE_WITH_PUBLISHED_DIGEST
ENERGY_PULL_POLICY=missing
ENERGY_ROOT=/opt/home-energy
PI_LAN_IP=192.168.1.2
ENERGY_HOSTNAME=home.energy
ENERGY_ADMIN_USERNAME=admin
HTTP_PORT=8080
```

5. Allow the image to be pulled from GHCR; `missing` downloads a new digest if absent. For a private package configure the Portainer registry first. Local-build users must instead set `ENERGY_PULL_POLICY=never`.
6. Deploy/update the stack and confirm the container becomes healthy.

For simpler manual upgrades, use `ENERGY_IMAGE=ghcr.io/mrcarlfarmer/home-energy-consumption:latest` and `ENERGY_PULL_POLICY=always`, then choose **Re-pull image and redeploy** when updating. The verified current-main build updates `latest` automatically, but the running Pi does not update until redeployed. See [latest and rollback guidance](registry-deployment.md#use-latest-for-simpler-manual-updates).

For Git-managed stacks use **Pull and redeploy** after the publication workflow completes, enabling image re-pulling. The repository stores the stack definition; your variables stay in Portainer and your database stays on the Pi.

Host mounts are the private data directory and read-only administrator password file. There are no certificate mounts. Do not put the Octopus key or password into the stack variables.

The stack retains the 140 MiB memory limit, no extra swap allowance, non-root user, read-only root filesystem, bounded logs and dropped capabilities. Keep capacity for the Pi OS, Docker, Portainer and other containers. Bind to the reserved LAN IP and use Docker-aware firewall/network restrictions; do not expose the published HTTP port on the internet.

## 5. Open the dashboard

Open **`http://home.energy:8080`**, not `https://`. Sign in with `admin` (or `ENERGY_ADMIN_USERNAME`) and your password. All telemetry, history and settings require login. Existing Octopus settings/history are retained. A persistent warning identifies unencrypted HTTP.

Sessions survive restarts and expire 12 hours after sign-in. Cookies are HttpOnly/SameSite=Strict but cannot be Secure in HTTP mode. Mutations require CSRF and a trusted Origin. HSTS is not sent over HTTP.

If a browser remembers HTTPS/HSTS for this hostname, use `http://192.168.1.2:8080` or remove only this hostname's remembered policy. Do not disable browser security globally.

The health probe uses loopback HTTP in this mode; it does not read certificates or disable validation for any HTTPS connection.

## Backups and later changes

Use the [backup and upgrade procedures](portainer-installation.md#8-backups-upgrades-and-recovery) with the current image and unchanged data directory. Backups contain secrets; reset the password to revoke restored sessions before serving restored data.

When upgrading a previously password-free stack, prepare the password file first, wait for image publication, then **Pull and redeploy** with image re-pulling. The username defaults to `admin` and is configured by `ENERGY_ADMIN_USERNAME`; it is not secret. Existing password hashes are retained. Username/password are mandatory in login requests; older browser clients must reload after upgrading.

To return to HTTPS, obtain the certificate files and use the [HTTPS installation guide](portainer-installation.md) and original stack. Never simply change URL schemes without also changing the transport, trusted Origins, port mapping and mounts. A reverse-proxy deployment is a separate configuration; this HTTP mode does not automatically trust forwarded headers.
