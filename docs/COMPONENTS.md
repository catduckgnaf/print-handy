# Component provenance and vulnerability inventory

Print Handy ships third-party software in its container image and Compose file. This page lists each pinned component, where the pin lives, and how the image is inventoried for known vulnerabilities.

## Pinned components

| Component | Pin | Where | Verification |
| --- | --- | --- | --- |
| Base image | `debian:13-slim` (moving tag, rebuilt per release) | `Dockerfile` | Release image is inventoried by SBOM (below) |
| OrcaSlicer | `2.5.0-rc`, revision `af2ffb8c…` | `docker/orca-prebuilt.sh` | SHA256 per architecture checked before extraction |
| go2rtc | `1.9.15`, revision `7c05066` | `docker/go2rtc-prebuilt.sh` | SHA256 per architecture checked before install |
| BambuStudio | commit `926a7192…` (`BAMBU_REF`) | `Dockerfile` | Build fails unless `git rev-parse HEAD` equals the ref |
| Spoolman | commit `bed3f14e…` (`SPOOLMAN_REF`) | `Dockerfile` | Build fails unless the checked-out commit equals the ref; `uv sync --locked` |
| uv (build only) | `0.9.5` | `Dockerfile` | Exact version |
| Python packages | `requirements.txt`, `requirements-test.txt` | repository root | `pip-audit` in `ci.yml` |
| cloudflared sidecar | `2026.10.7` plus manifest digest | `docker-compose.yml` (`CLOUDFLARED_IMAGE` overrides) | Digest is the multi-architecture index; never `latest` |

`latest` and `develop` of the Print Handy image itself remain a documented convenience for people who want the newest release. Pin a version tag or digest for a reproducible install.

## Image SBOM and vulnerability inventory

The `Image inventory` workflow (`.github/workflows/image-inventory.yml`) runs weekly and on demand. It pulls a published image (default `ghcr.io/catduckgnaf/print-handy:latest`, or the `image` input), writes an SPDX SBOM with Syft, and scans that SBOM with Trivy. Both files are uploaded as workflow artifacts and a severity summary is written to the run page.

The scan covers operating system packages and the bundled binaries Syft can identify; `pip-audit` in `ci.yml` stays the check for Python requirements. The inventory is report only: it never fails a build or publishes anything, because most findings in a Debian base are unfixed or unreachable.

## Review cadence and exceptions

- Read the weekly report and update pins when a fixed upstream release exists. Bump the pin, its checksum or digest, and this table together.
- Re-check the cloudflared pin against the latest upstream release at least monthly, and after any cloudflared advisory. Look at the release notes for flag or behavior changes before moving the tag.
- Accepted findings (no fix available, or not reachable in this image) are recorded in the PR or issue that accepts them, with the component, identifier, reason and a date to re-review. Do not suppress findings in the workflow.
