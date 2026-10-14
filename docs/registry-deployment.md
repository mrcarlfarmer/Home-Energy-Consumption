# Automated GHCR publishing and Portainer deployment

GitHub Actions builds and publishes the application so the Pi only downloads an image. No Git, Node, Python development tools or Buildx are needed on the Pi. Docker Standalone, Portainer, 64-bit ARM64 Linux, local persistent storage and outbound HTTPS access to GHCR are required.

## Build and publication

The `Verify` workflow runs backend checks, browser scenarios and an AMD64/ARM64 container build. Only after **both verification jobs succeed**, a push to `main` publishes to:

```text
ghcr.io/mrcarlfarmer/home-energy-consumption:sha-<full-40-character-commit>
```

Pull requests and other branches never publish. You can also run `Verify` manually from Actions on `main`; other manually selected branches only validate. After publishing a verified image, the workflow updates `:latest` only if its commit is still the current `main` head. Publication jobs are serialized, preventing an older build or rerun from overwriting a newer latest alias. If main advances during verification, the older commit image remains available but does not move `latest`. Failed verification leaves the previous alias unchanged. There are no release-version aliases.

The publishing job uses GitHub's automatically provided `${{ secrets.GITHUB_TOKEN }}` with job-scoped `packages: write`. No personal publishing token or manually created repository secret is required. Source/revision labels link the package to its repository; builds include provenance and SBOM attestations. The successful run's **Summary** lists the commit tag and the exact multi-architecture manifest digest. Digest references are preferable because a tag can be overwritten by a rerun.

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

The source repository and the existing `home-energy-consumption` package are currently public. The pipeline does not change repository/package visibility. Images contain application code, not your runtime credentials. Check actual package visibility before relying on private access.

**GitHub does not allow changing a public package back to private.** Public images cannot be recalled. If you make the repository private, the workflow checks anonymous package access **before uploading** and refuses to publish private source to the current public package. It checks again after publication. Network/registry errors fail explicitly rather than being treated as privacy.

Before your first private-source publication, set the Actions repository variable **`GHCR_PACKAGE`** to a new package path, for example `mrcarlfarmer/home-energy-internal`, and ensure it is private and grants this repository publishing access. The image name defaults to `github.repository` when the variable is absent. New GHCR packages normally start private, but verify that; do not assume repository visibility controls it. Do not reuse the already-public `home-energy-consumption` or `home-energy-consumption-private` packages. The latter was created during rollout checking and also allowed anonymous access; neither contains runtime secrets. A private-source run intentionally fails if its selected package is public.

**Private images (recommended if you plan to make the repository private):**

1. In GitHub Packages, confirm the package is linked to this repository and its Actions access allows this repository to publish. If an existing package gives `permission_denied: write_package`, fix its package access rather than adding a broad personal token.
   Confirm visibility is **Private**. If a package has already been made public, use a new package name rather than attempting to make it private again.
2. Create a **classic PAT** with `read:packages`, owned by an account allowed to read the package. Authorize SSO if required. Do not use a fine-grained PAT for GHCR package authentication or grant write/delete scopes to Portainer.
3. In Portainer, open **Registries > Add registry > Custom registry**. Set name `GitHub Container Registry`, URL `ghcr.io`, enable authentication and supply the GitHub username and PAT as the password.
4. Ensure the Portainer user deploying the stack has access to that registry. Store the PAT there, not in YAML, Git, the image or the app's settings. Rotate it before expiry.

Public images can be pulled anonymously, but public package visibility is a separate, deliberate GitHub setting and cannot be reversed. Making the repository private later does not make a previously public package private. Review repository and package visibility independently; already downloaded images cannot be recalled.

## Install on the Pi

Both stacks now require username/password authentication. Use the [HTTP guide](portainer-http-installation.md) to prepare its private password file before upgrading, or the [HTTPS guide](portainer-installation.md). Default username is `admin`, overridden with `ENERGY_ADMIN_USERNAME`; no password is stored in GitHub or image layers. Keep DNS `home.energy -> 192.168.1.2`.

For HTTP, prepare both directories and the password file described in its guide:

```sh
sudo install -d -m 755 /opt/home-energy
sudo install -d -m 700 -o 10001 -g 10001 /opt/home-energy/data /opt/home-energy/secrets
```

1. Open the successful `Verify` run in GitHub Actions and copy its **Exact Portainer image** from the Summary. Wait for publication to finish; a source commit existing does not mean its image exists.
2. Select the Pi's Portainer Docker Standalone environment. Add/edit the `home-energy` stack using [deploy/portainer-http-stack.yml](../deploy/portainer-http-stack.yml). Do not start a second collector against the same data or meter.
3. Set the variables below, replacing the digest placeholder with the published reference:

```dotenv
ENERGY_IMAGE=ghcr.io/mrcarlfarmer/home-energy-consumption@sha256:REPLACE_WITH_PUBLISHED_DIGEST
ENERGY_PULL_POLICY=missing
ENERGY_ROOT=/opt/home-energy
PI_LAN_IP=192.168.1.2
ENERGY_HOSTNAME=home.energy
HTTP_PORT=8080
```

For HTTPS use the original stack and `HTTPS_PORT=8443` instead, plus its existing private file preparation. Both stacks request `linux/arm64`; GHCR's multi-architecture manifest supplies the appropriate image. Neither embeds runtime credentials.

4. Deploy/update the stack. The default `missing` pull policy downloads the chosen reference if absent, reusing an identical pinned image if present. Portainer may also offer **Re-pull image and redeploy**; enable it when deliberately refreshing a tag. Do not set `never` for an image that has not been downloaded.
5. Confirm the container is healthy and running the intended reference. Open `http://home.energy:8080`, check settings/history and verify fresh telemetry with polling enabled. A successful deployment request alone is not a health check.

Both Portainer stacks set `container_name: home-energy`, rather than Compose's generated `home-energy-energy-1`. For an existing Web editor stack, add this line under the `energy:` service and update the same stack; the container is recreated but the bind-mounted data stays intact. Do not rename it only in the Containers view, as the stack configuration should remain the source of truth. The fixed name permits only one such container on a Docker endpoint; do not deploy the HTTP and HTTPS alternatives simultaneously.

**HTTP remains trusted-LAN-only:** login is required for the dashboard/API, but passwords and sessions travel unencrypted. A private registry does not provide transport encryption. Do not publicly expose Portainer or the app.

### Deploy the stack from the Git repository

The same Compose files work with Portainer's **Git repository** option; no `build:` section, relative data mounts or additional YAML file is needed. Portainer downloads the stack definition from Git and the application image from GHCR separately. The repository URL/branch/path are Portainer settings, not Compose properties.

For a new stack select the Pi's Docker Standalone environment, then **Stacks > Add stack > Git repository**, with name `home-energy`. Depending on Portainer version, enter the repository fields directly or first create/select a Git source under App Delivery:

| Portainer field | Value |
|---|---|
| Repository URL | `https://github.com/mrcarlfarmer/Home-Energy-Consumption.git` |
| Repository reference | `refs/heads/main` (or select the `main` branch) |
| Compose path | `deploy/portainer-http-stack.yml` |
| Additional Compose files | None |
| GitOps/automatic updates | Disabled initially |

For the protected HTTPS alternative, use `deploy/portainer-stack.yml` instead and prepare its private files. Compose paths use forward slashes because they are repository-relative Portainer inputs.

Set the deployment variables in Portainer's **Environment variables** section, not GitHub Actions secrets:

```dotenv
ENERGY_IMAGE=ghcr.io/mrcarlfarmer/home-energy-consumption:latest
ENERGY_PULL_POLICY=always
ENERGY_ROOT=/opt/home-energy
PI_LAN_IP=192.168.1.2
ENERGY_HOSTNAME=home.energy
ENERGY_ADMIN_USERNAME=admin
HTTP_PORT=8080
```

For exact-version deployment, use a published digest and `ENERGY_PULL_POLICY=missing`. Keep the data directory on the Pi, outside Portainer's Git checkout. Deploy and verify container `home-energy` is healthy and existing settings/history remain present.

**Migrating an existing Web editor stack:** do not deploy another stack alongside it. First take a consistent database backup, record its variables and image digest, and save its current YAML. If your Portainer version offers a supported source-change option, use it for the existing stack. Otherwise remove the existing stack/its container in Portainer and recreate `home-energy` using the Git fields above, with the exact same `/opt/home-energy/data` bind mount. This causes a brief collection outage. Do not delete the host data directory, select volume/data removal, or start a second collector. The bind-mounted database survives container/stack recreation; the Git checkout is not where it is stored.

To update a Git-managed stack, wait for the GitHub publication run to finish, then select **Stacks > home-energy > Pull and redeploy**, enabling **Re-pull image and redeploy**. This fetches both the new YAML and current image. Edit Compose changes in the repository rather than keeping a separate pasted copy.

Leave automatic GitOps polling/webhooks off for now: a main commit may reach Portainer before its verified image publication finishes, and changes to `latest` alone are not a reliable Git-change trigger. Repository-backed configuration does not make image deployment automatic.

If you make the source repository private, configure read-only Git credentials in Portainer's Git source/repository authentication settings (for example a fine-grained PAT with Contents read access to this repository). These are separate from GHCR's classic `read:packages` registry token. Neither token belongs in the stack YAML; the Octopus key remains runtime configuration, not a Git credential.

### Use latest for simpler manual updates

Instead of changing the image digest on every upgrade, set:

```dotenv
ENERGY_IMAGE=ghcr.io/mrcarlfarmer/home-energy-consumption:latest
ENERGY_PULL_POLICY=always
```

Use your selected `GHCR_PACKAGE` image path instead if you publish to a different private package. On a deliberate stack deployment/redeployment, Docker pulls the current alias. In Portainer select **Re-pull image and redeploy** when updating the stack. This does not watch the registry, restart the running app when a build completes, or schedule automatic updates.

`latest` is a moving alias, not an upgrade safety guarantee. Back up the database and record the currently running digest before redeploying. Use the old digest for rollback rather than `latest`; pinned digests remain the more reproducible deployment choice.

## Upgrade and rollback

Publishing is automated; deployment is intentionally manual. There are no internet-facing webhooks or automatic Pi updates.

Take a private consistent database backup using the [backup procedure](portainer-installation.md#8-backups-upgrades-and-recovery). For a one-off CLI backup, use the image currently running on the Pi; a private image must already be cached or the Pi's CLI needs its own registry login (Portainer credentials are not automatically shared with `docker login`).

Record the old image digest. Change only `ENERGY_IMAGE` to a new successful run's digest, preserve the data mount and other variables, and update the same stack. Confirm health, telemetry and retained history. Reload older browser clients after this username-login upgrade.

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
