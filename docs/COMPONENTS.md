# Component provenance and vulnerability inventory

Print Handy ships third-party software in its container image and Compose file. This page lists each pinned component, where the pin lives, and how the image is inventoried for known vulnerabilities.

## Pinned components

| Component | Pin | Where | Verification |
| --- | --- | --- | --- |
| Base image | `debian:13-slim` (moving tag, rebuilt per release) | `Dockerfile` | Release image is inventoried by SBOM (below) |
| OrcaSlicer | `2.5.0`, revision `40ecc66f…` | `docker/orca-prebuilt.sh` | SHA256 per architecture checked before extraction |
| go2rtc | `1.9.15`, revision `7c05066` | `docker/go2rtc-prebuilt.sh` | SHA256 per architecture checked before install |
| BambuStudio | commit `56a57d00…` (`BAMBU_REF`) | `Dockerfile` | Build fails unless `git rev-parse HEAD` equals the ref |
| Spoolman | commit `bed3f14e…` (`SPOOLMAN_REF`) | `Dockerfile` | Build fails unless the checked-out commit equals the ref; `uv sync --locked` |
| uv (build only) | `0.9.5` | `Dockerfile` | Exact version |
| OpenSCAD | Debian 13 package (distro version, unpinned) | `Dockerfile` runtime stage; `ci.yml` offline legs install the same package | Parity test in `tests/test_upstream_version_parity.py`: both surfaces install it from Debian 13, so the design/render tests prove the version the image ships |
| Tesseract OCR | Debian 13 package (distro version, unpinned; `tesseract-ocr` carries the `tesseract-ocr-eng` language data as a dependency) | `Dockerfile` runtime stage; `ci.yml` offline legs install the same package | Same parity test as OpenSCAD |
| Native bounds toolchain (build only) | Rust `1.99.1`, `maturin==1.16.0`, crate dependencies in `native/bounds/Cargo.lock` | `Dockerfile` bounds-build stage; `ci.yml` native-bounds job and offline legs | Parity test in `tests/test_upstream_version_parity.py` (all surfaces carry the same toolchain and maturin pins, and every wheel build uses `--locked`) |
| Python packages | `pyproject.toml` | repository root | `pip-audit` in `ci.yml` |
| cloudflared sidecar | `2026.10.7` plus manifest digest | `docker-compose.yml` (`CLOUDFLARED_IMAGE` overrides) | Digest is the multi-architecture index; never `latest` |
| Discord Embedded App SDK | `2.5.0` | `print_handy/discord_embedded_app_sdk.js` (vendored single-file bundle of the MIT npm package `@discord/embedded-app-sdk`, served by `print_handy/live_activity.py` from its own origin) | Vendored, never fetched at runtime (the Activity CSP allows no external hosts); a bump replaces the file whole at a new pin and is not auto-bumped — see `NON_BUMPED_COMPONENTS` in `scripts/bump_upstream.py` |

`latest` and `develop` of the Print Handy image itself remain a documented convenience for people who want the newest release. Pin a version tag or digest for a reproducible install.

## Image SBOM and vulnerability inventory

The `Image inventory` workflow (`.github/workflows/image-inventory.yml`) runs weekly and on demand. It pulls a published image (default `ghcr.io/catduckgnaf/print-handy:latest`, or the `image` input), writes an SPDX SBOM with Syft, and scans that SBOM with Trivy. Both files are uploaded as workflow artifacts and a severity summary is written to the run page.

The scan covers operating system packages and the bundled binaries Syft can identify; `pip-audit` in `ci.yml` stays the check for Python requirements. The inventory is report only: it never fails a build or publishes anything, because most findings in a Debian base are unfixed or unreachable.

## Review cadence and exceptions

- Read the weekly report and update pins when a fixed upstream release exists. Bump the pin, its checksum or digest, and this table together.
- The `Slicer test (bambu)` CI leg runs a Flathub Bambu Studio build pinned by ostree commit (2.8.2.61 today), which does not move with `BAMBU_REF`. When a bump changes the Bambu generation (the first two version components), either pin a Flathub build of the new generation in `ci.yml` and in `tests/test_ci_bambu_workflow.py` (`FLATPAK_BAMBU_VERSION`, `FLATPAK_BAMBU_COMMIT`), or record a dated verdict for the new pair in that test's `RECORDED_DIVERGENCES`. The test fails until one of them is done.
- Re-check the cloudflared pin against the latest upstream release at least monthly, and after any cloudflared advisory. Look at the release notes for flag or behavior changes before moving the tag.
- Accepted findings (no fix available, or not reachable in this image) are recorded in the PR or issue that accepts them, with the component, identifier, reason and a date to re-review. Do not suppress findings in the workflow.
