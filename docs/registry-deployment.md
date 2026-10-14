# Automated GHCR publishing and Portainer deployment

GitHub Actions builds and publishes the application so the Pi only downloads an image. No Git, Node, Python development tools or Buildx are needed on the Pi. Docker Standalone, Portainer, 64-bit ARM64 Linux, local persistent storage and outbound HTTPS access to GHCR are required.

## Build and publication

The `Verify` workflow runs backend checks, browser scenarios and an AMD64/ARM64 container build. Only after **both verification jobs succeed**, a push to `main` publishes to:

```text
ghcr.io/mrcarlfarmer/home-energy-consumption-private:sha-<full-40-character-commit>
```

Pull requests and other branches never publish. You can also run `Verify` manually from Actions on `main`; other manually selected branches only validate. There are no release-tag or `latest` aliases: each deployment selects a verified commit explicitly, rather than silently following a moving image tag.

The publishing job uses GitHub's automatically provided `${{ secrets.GITHUB_TOKEN }}` with job-scoped `packages: write`. No personal publishing token or manually created repository secret is required. Source/revision labels link the package to its repository; builds include provenance and SBOM attestations. After publishing, a check fails the job if the image can be downloaded anonymously. The successful run's **Summary** lists the commit tag and the exact multi-architecture manifest digest. Digest references are preferable because a tag can be overwritten by a rerun.

## Secret boundaries

| Credential | Where it belongs |
|---|---|
| Image publishing token | GitHub Actions' automatic `GITHUB_TOKEN`; never source code |
| Private image pull token | Portainer's registry credentials; a classic GitHub PAT with `read:packages` and access to the package |
| Octopus API key | Enter in the running dashboard; stored in the Pi's private SQLite database |
| Optional dashboard password/TLS key | Private files on the Docker endpoint, mounted only when using protected HTTPS |

The build and synthetic tests do **not** call Octopus and do not need its API key. Do not add it to Docker build arguments, build secrets, workflow environment variables or image layers. Moving the repository to private does not make an embedded credential safe. The workflow publishes generic code, not your database or runtime settings; `.dockerignore` excludes `.env`, `secrets`, `data` and SQLite files.

GitHub Actions secrets are not a runtime retrieval service for the Pi. For stronger separation from database backups, a future runtime-only secret-file mode could read an owner-only (`0400`, UID 10001) file mounted read-only outside `/data`, instead of accepting/persisting the key through Settings. **That mode is not implemented by this publishing change**; currently the key remains in the private database and its backups. Docker/root administrators can read either form. A managed secret service can add access auditing and centralized rotation, at the cost of extra infrastructure. Do not delete your working database key until a replacement runtime source is implemented and verified.

If a future workflow genuinely needs an external API credential, add it through **Repository Settings > Secrets and variables > Actions**, scope access to that job and never print it. This publishing pipeline deliberately has no such dependency. GitHub repository secrets are not automatically delivered to Portainer containers.

## Registry visibility and access

The workflow now publishes to the separate `home-energy-consumption-private` package, not the earlier publicly pullable `home-energy-consumption` package. New GHCR packages default to private; the workflow does not make packages public or change repository visibility. Check actual package visibility before relying on private access.

**GitHub does not allow changing a public package back to private.** The original public package's images contain no runtime credentials, but they cannot be recalled. Future builds use the new package name so making the source repository private does not leave future images publishing to the old public package. Do not change the new package to public if you want to preserve this boundary.

**Private images (recommended if you plan to make the repository private):**

1. In GitHub Packages, confirm the package is linked to this repository and its Actions access allows this repository to publish. If an existing package gives `permission_denied: write_package`, fix its package access rather than adding a broad personal token.
   Confirm visibility is **Private**. If a package has already been made public, use a new package name rather than attempting to make it private again.
2. Create a **classic PAT** with `read:packages`, owned by an account allowed to read the package. Authorize SSO if required. Do not use a fine-grained PAT for GHCR package authentication or grant write/delete scopes to Portainer.
3. In Portainer, open **Registries > Add registry > Custom registry**. Set name `GitHub Container Registry`, URL `ghcr.io`, enable authentication and supply the GitHub username and PAT as the password.
4. Ensure the Portainer user deploying the stack has access to that registry. Store the PAT there, not in YAML, Git, the image or the app's settings. Rotate it before expiry.

Public images can be pulled anonymously, but public package visibility is a separate, deliberate GitHub setting and cannot be reversed. Making the repository private later does not make a previously public package private. Review repository and package visibility independently; already downloaded images cannot be recalled.

## Install on the Pi

Use the [HTTP/no-login guide](portainer-http-installation.md) for the chosen LAN deployment, or the [protected HTTPS guide](portainer-installation.md) for certificate/password preparation. Keep DNS `home.energy -> 192.168.1.2`.

For the HTTP setup, the only required host directory is:

```sh
sudo install -d -m 755 /opt/home-energy
sudo install -d -m 700 -o 10001 -g 10001 /opt/home-energy/data
```

1. Open the successful `Verify` run in GitHub Actions and copy its **Exact Portainer image** from the Summary. Wait for publication to finish; a source commit existing does not mean its image exists.
2. Select the Pi's Portainer Docker Standalone environment. Add/edit the `home-energy` stack using [deploy/portainer-http-stack.yml](../deploy/portainer-http-stack.yml). Do not start a second collector against the same data or meter.
3. Set the variables below, replacing the digest placeholder with the published reference:

```dotenv
ENERGY_IMAGE=ghcr.io/mrcarlfarmer/home-energy-consumption-private@sha256:REPLACE_WITH_PUBLISHED_DIGEST
ENERGY_PULL_POLICY=missing
ENERGY_ROOT=/opt/home-energy
PI_LAN_IP=192.168.1.2
ENERGY_HOSTNAME=home.energy
HTTP_PORT=8080
```

For HTTPS use the original stack and `HTTPS_PORT=8443` instead, plus its existing private file preparation. Both stacks request `linux/arm64`; GHCR's multi-architecture manifest supplies the appropriate image. Neither embeds runtime credentials.

4. Deploy/update the stack. The default `missing` pull policy downloads the chosen reference if absent, reusing an identical pinned image if present. Portainer may also offer **Re-pull image and redeploy**; enable it when deliberately refreshing a tag. Do not set `never` for an image that has not been downloaded.
5. Confirm the container is healthy and running the intended reference. Open `http://home.energy:8080`, check settings/history and verify fresh telemetry with polling enabled. A successful deployment request alone is not a health check.

**HTTP/no-login remains a trusted-LAN-only deployment:** anyone who can reach it can read data/change settings, and browser traffic is unencrypted. A private GitHub repository/image does not add dashboard authentication or encryption. Do not publicly expose Portainer or the app.

## Upgrade and rollback

Publishing is automated; deployment is intentionally manual. There are no internet-facing webhooks or automatic Pi updates.

Take a private consistent database backup using the [backup procedure](portainer-installation.md#8-backups-upgrades-and-recovery). For a one-off CLI backup, use the image currently running on the Pi; a private image must already be cached or the Pi's CLI needs its own registry login (Portainer credentials are not automatically shared with `docker login`).

Record the old image digest. Change only `ENERGY_IMAGE` to a new successful run's digest, preserve the data mount and other variables, and update the same stack. Confirm health, telemetry and retained history. In password-free mode reload an open browser after restarting to refresh its CSRF token.

Retain old image versions and compatible database backups. Reverting the reference is sufficient only when the database schema remains compatible; a schema-changing rollback may require restoring its matching backup while stopped. Never delete the data directory or run two app containers against it. Do not remove old GHCR versions that you still need for rollback.

## Local-build fallback

The existing [native Pi build instructions](portainer-installation.md#2-build-the-image-on-the-pi) remain an offline/registry-independent alternative. Use a local versioned image name and **`ENERGY_PULL_POLICY=never`**. This prevents Docker trying to find your local tag on Docker Hub. Keep that policy when using the image-transfer route.

## Troubleshooting

| Symptom | Check |
|---|---|
| Publication job skipped | It only runs on `main` push/manual runs, after both verification jobs pass |
| GHCR write denied | Job has `packages: write`; package Actions access includes this repository; account/package policy permits publishing |
| Unauthorized/private image pull | Correct Portainer registry username, classic PAT, package read access, SSO and token expiry |
| Manifest unknown | Use a completed run's published tag/digest, not a commit whose publication is pending/failed |
| No ARM64 manifest | Use this pipeline's multi-architecture application reference, not an attestation manifest or old AMD64 image |
| Old application after update | Check actual image reference/digest, change `ENERGY_IMAGE`, and recreate the stack |

References: [GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry), [Portainer custom registries](https://docs.portainer.io/admin/registries/add/custom), [Portainer stack updates](https://docs.portainer.io/user/docker/stacks/edit).
