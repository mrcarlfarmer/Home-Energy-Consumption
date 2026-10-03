# Deployment

## Prerequisites

Use a Raspberry Pi with a 64-bit Linux OS, Docker Engine and the Docker Compose plugin. Keep host time synchronized. Store `/data` on a local filesystem, preferably SSD or high-endurance storage, not NFS/SMB. Reserve capacity for indefinite raw retention and backups.

All commands below target the Linux deployment host. The initial image build needs internet access; the installed dashboard's static assets do not.

## Password and certificate

Create a private directory outside version control. `.gitignore` excludes the example `secrets` directory, but do not rely on Git alone to protect files.

```sh
mkdir -m 700 secrets
umask 077
read -r -s -p "Dashboard password (12+ characters): " PASSWORD
printf '\n'
printf '%s\n' "$PASSWORD" > secrets/admin-password
unset PASSWORD
```

Use a long unique password. It bootstraps the administrator hash only on the first database initialization. Updating this file after initialization does not change an existing password; use the reset procedure in the operations guide.

Obtain a certificate from your trusted local CA, with SANs for the DNS names and/or IP addresses that you will actually visit. For example, using an already installed `mkcert`:

```sh
mkcert -install
mkcert -cert-file secrets/energy.pem -key-file secrets/energy-key.pem energy.home.arpa 192.168.1.50
```

Configure local DNS so `energy.home.arpa` resolves to the Pi. Install **only the CA certificate**, never its private key, into the trust store of each browser device. Never copy the CA private key into the repository/container. A self-signed certificate without appropriate client trust is not a production substitute.

The container runs as UID/GID 10001. Secret bind mounts must be readable by that UID. For Linux with the example files:

```sh
sudo chown 10001:10001 secrets/admin-password secrets/energy-key.pem
sudo chmod 400 secrets/admin-password secrets/energy-key.pem
chmod 644 secrets/energy.pem
```

Compose file-backed secrets do not reliably remap ownership. Verify actual host permissions; do not make the private key/password globally readable to solve a mount issue. Keep the parent directory private.

## Configure and start

Copy `.env.example` to `.env` and set:

| Variable | Meaning |
|---|---|
| `LAN_BIND_ADDRESS` | The Pi LAN IP to bind, not `0.0.0.0` |
| `APP_ALLOWED_HOSTS` | Comma-separated exact hostnames/IPs, without scheme/port |
| `APP_ALLOWED_ORIGINS` | Comma-separated HTTPS origins including `:8443` |
| `ADMIN_PASSWORD_SECRET_FILE` | Host path to the bootstrap password file |
| `TLS_CERTIFICATE_FILE` | Host path to the certificate/chain |
| `TLS_PRIVATE_KEY_FILE` | Host path to the leaf private key |

The first allowed hostname must appear in the certificate SANs. The health probe connects over loopback while validating that hostname against the mounted certificate; it never disables certificate validation. It explicitly trusts the mounted certificate as a partial-chain anchor, so a locally issued leaf works without shipping the CA's private key.

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail 50 energy
```

The named volume receives `/data`'s private UID 10001 ownership on initial creation. If using a pre-existing/bind-mounted data directory instead, provision correct ownership before starting.

Only port 8443 is published. Restrict it with the host firewall to the trusted LAN. Do not create router port-forwarding rules. For future remote access use an authenticated VPN/HTTPS access layer and deliberately revise the trusted Host/Origin configuration.

## First login

1. Open the configured HTTPS address and use the bootstrap password.
2. Enter the Octopus API key and account number in Settings.
3. Test connectivity to list electricity meters. Select the intended EUI-64 ID; test again when the rate governor permits.
4. Save settings, then click **Start polling** in the Polling panel beside live demand. The same panel lets you stop collection and save a polling interval (30-3,600 seconds, default 45). These controls update the saved settings immediately; saving an interval alone does not enable polling. The equivalent controls remain available in Settings and take effect when **Save settings** is clicked.

Connectivity tests do not save credentials or enable collection. Test requests share the telemetry quota governor; a recent test/poll can require waiting before the next test. An accessible meter with no recent data is reported separately from a successful telemetry retrieval.

The demand and inverter-sizing chart offers **5 min**, **15 min**, **30 min**, **1 hour**, **24 hours**, **7 days** and **30 days**, plus a custom range. Presets end at the current time and continue moving forward with live updates.

## Resource policy and images

The Compose limit is **140 MiB = 146,800,640 bytes**, below 150 decimal MB. Swap is not available to hide an oversized process where Docker enforces the configured limits. The root filesystem is read-only, `/tmp` is an 8 MiB tmpfs, all capabilities are dropped and new privileges are disabled.

This limit is a guardrail, not a native Pi benchmark. See validation results before treating the resource target as certified. Docker daemon/host OS and the client browser consume additional memory outside the application container.

Buildx can build both targets in CI. To load a single target into a classic local Docker image store:

```sh
docker buildx build --platform linux/arm64 --load -t home-energy-monitor:arm64 .
```

For an AMD64 host, ARM64 execution/building requires native remote hardware or a QEMU-capable builder. Do not interpret emulated performance as real Pi performance.
