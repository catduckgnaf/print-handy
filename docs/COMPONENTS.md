# Component provenance and vulnerability inventory

Print Handy ships third-party software in its container image and Compose file. This page lists each pinned component, where the pin lives, and how the image is inventoried for known vulnerabilities.

## Pinned components

| Component | Pin | Where | Verification | License | Source |
| --- | --- | --- | --- | --- | --- |
| Base image | `debian:13-slim` (moving tag, rebuilt per release) | `Dockerfile` | Release image is inventoried by SBOM (below) | Mixed; each Debian package carries its own license in `/usr/share/doc/<package>/copyright` inside the image (unverified here) | <https://www.debian.org/legal/licenses/> |
| OrcaSlicer | `2.5.0`, revision `40ecc66f…` | `docker/orca-prebuilt.sh` | SHA256 per architecture checked before extraction | AGPL-3.0 (upstream `LICENSE.txt`; the AppImage also bundles its own third-party libraries, unverified) | <https://github.com/OrcaSlicer/OrcaSlicer> |
| go2rtc | `1.9.15`, revision `7c05066` | `docker/go2rtc-prebuilt.sh` | SHA256 per architecture checked before install | MIT | <https://github.com/AlexxIT/go2rtc> |
| BambuStudio | commit `56a57d00…` (`BAMBU_REF`) | `Dockerfile` | Build fails unless `git rev-parse HEAD` equals the ref | AGPL-3.0 (upstream `LICENSE`). Built only with `WITH_BAMBU=true`; the published image leaves it out | <https://github.com/bambulab/BambuStudio> |
| Spoolman | v0.27.1, commit `ff8efd51…` (`SPOOLMAN_REF`) | `Dockerfile` | Build fails unless the checked-out commit equals the ref; `uv sync --locked` | MIT | <https://github.com/Donkie/Spoolman> |
| uv (build only) | `0.9.5` | `Dockerfile` | Exact version | Apache-2.0 OR MIT upstream (GitHub reports Apache-2.0); not shipped in the image | <https://github.com/astral-sh/uv> |
| OpenSCAD | Debian 13 package (distro version, unpinned) | `Dockerfile` runtime stage; `ci.yml` offline legs install the same package | Parity test in `tests/test_upstream_version_parity.py`: both surfaces install it from Debian 13, so the design/render tests prove the version the image ships | GPL-2.0-or-later with a CGAL linking exception (upstream `COPYING`); the Debian package's own copyright file is unverified | <https://github.com/openscad/openscad> |
| Tesseract OCR | Debian 13 package (distro version, unpinned; `tesseract-ocr` carries the `tesseract-ocr-eng` language data as a dependency) | `Dockerfile` runtime stage; `ci.yml` offline legs install the same package | Same parity test as OpenSCAD | Apache-2.0 (upstream); the Debian package's own copyright file is unverified | <https://github.com/tesseract-ocr/tesseract> |
| Native bounds toolchain (build only) | Rust `1.99.1`, `maturin==1.16.0`, crate dependencies in `native/bounds/Cargo.lock` | `Dockerfile` bounds-build stage; `ci.yml` native-bounds job and offline legs | Parity test in `tests/test_upstream_version_parity.py` (all surfaces carry the same toolchain and maturin pins, and every wheel build uses `--locked`) | Rust toolchain MIT OR Apache-2.0, maturin Apache-2.0 OR MIT, pyo3 MIT OR Apache-2.0; the crate itself is AGPL-3.0-only (`native/bounds/Cargo.toml`). The 20 crates in `Cargo.lock` are not individually checked: unverified | <https://github.com/PyO3/maturin> |
| Python packages | `pyproject.toml` | repository root | `pip-audit` in `ci.yml` | See the Python packages table below | <https://pypi.org/> |
| cloudflared sidecar | `2026.10.7` plus manifest digest | `docker-compose.yml` (`CLOUDFLARED_IMAGE` overrides) | Digest is the multi-architecture index; never `latest` | Apache-2.0. A separate image run beside Print Handy, not part of the Print Handy image | <https://github.com/cloudflare/cloudflared> |
| Discord Embedded App SDK | `2.5.0` | `print_handy/discord_embedded_app_sdk.js` (vendored single-file bundle of the MIT npm package `@discord/embedded-app-sdk`, served by `print_handy/live_activity.py` from its own origin) | Vendored, never fetched at runtime (the Activity CSP allows no external hosts); a bump replaces the file whole at a new pin and is not auto-bumped (see `NON_BUMPED_COMPONENTS` in `scripts/bump_upstream.py`) | MIT | <https://github.com/discord/embedded-app-sdk> |

`latest` and `develop` of the Print Handy image itself remain a documented convenience for people who want the newest release. Pin a version tag or digest for a reproducible install.

## Other bundled and downloaded parts

These are part of what the image runs or fetches but are not pinned by their own build script, so they have no row above.

| Component | How it gets there | License | Source |
| --- | --- | --- | --- |
| ffmpeg and its libraries (including `libx264`) | Debian 13 package, installed in the `Dockerfile` runtime stage | A mix of LGPL-2.1-or-later and GPL-2.0-or-later parts; Debian's build uses the GPL parts, so the shipped binaries are GPL-2.0-or-later. Verified against the Debian 13 `ffmpeg` 7.1.5 package copyright file (also at `/usr/share/doc/ffmpeg/copyright` inside the image), which lists the per-library terms | <https://www.debian.org/> package `ffmpeg`, upstream <https://ffmpeg.org/> |
| Piper text-to-speech and voice models | Never bundled: Wrapped narration runs a binary and voice model the operator supplies at `[paths] piper` and `[paths] piper_model` (operator-supplied, not shipped in the image or the wheel) | The Piper binary is MIT (upstream `LICENSE.md`). Each voice model carries the license of the dataset it was trained on, stated in its MODEL_CARD in `rhasspy/piper-voices`; they vary by voice (CC0, CC BY variants, public domain and others), so check the card for the voice you use | <https://github.com/rhasspy/piper> |
| ONNX Runtime (`onnxruntime`) | Python dependency (CPU build, the published image) | MIT. The optional `ORT_FLAVOR=cuda` build (`onnxruntime-gpu[cuda,cudnn]`) also pulls NVIDIA CUDA and cuDNN user-space libraries that are under NVIDIA's own license terms, not MIT: unverified here, and that flavor is not published | <https://github.com/microsoft/onnxruntime> |
| Failure-detector model weights | Downloaded once at run time when `vision.local_detector_enabled` is set, SHA-256 checked; never in the image (`print_handy/hermes/local_vision.py`) | AGPL-3.0, as stated in this repository (`local_vision.py` and the Web UI setting text) for the Obico project. The upstream repository's `LICENSE` is AGPL-3.0 and its `ml_api/model/` folder holds only the weights' URL and checksum, so the weights carry no separate license text there: unverified beyond that | <https://github.com/TheSpaghettiDetective/obico-server> |
| Print Handy itself | This repository | AGPL-3.0-only from the release after 2.2.7; 2.2.7 and earlier were MIT (see `NOTICE`) | <https://github.com/catduckgnaf/print-handy> |

### Python packages

Direct runtime dependencies from `pyproject.toml`, with the license each published package declares in its own metadata (read from the installed packages, versions as installed on 2026-11-09). Their own dependencies are not listed here: unverified.

| Package | License |
| --- | --- |
| jinja2 | BSD-3-Clause (PyPI classifier "BSD License") |
| zxing-cpp | Apache-2.0 |
| paho-mqtt | EPL-2.0 OR BSD-3-Clause |
| onnxruntime | MIT |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| pillow | MIT-CMU |
| pyyaml | MIT |
| websockets | BSD-3-Clause |
| discord.py | MIT |

### Where the table ships

The image carries this page at `/usr/share/doc/print-handy/COMPONENTS.md` beside `NOTICE` and `LICENSE`. The AGPL asks that a user of a network service can get its source: the source for the Print Handy code is <https://github.com/catduckgnaf/print-handy>, and the image's `org.opencontainers.image.source` label names it. The licenses above are what the maintainers could confirm against each upstream; where a cell says "unverified", read the license file in the image or upstream before relying on it.

## Image SBOM and vulnerability inventory

The `Image inventory` workflow (`.github/workflows/image-inventory.yml`) runs weekly and on demand. It pulls a published image (default `ghcr.io/catduckgnaf/print-handy:latest`, or the `image` input), writes an SPDX SBOM with Syft, and scans that SBOM with Trivy. Both files are uploaded as workflow artifacts and a severity summary is written to the run page.

The scan covers operating system packages and the bundled binaries Syft can identify; `pip-audit` in `ci.yml` stays the check for Python requirements. The inventory is report only: it never fails a build or publishes anything, because most findings in a Debian base are unfixed or unreachable.

Releases carry their own SBOM (#1158): a publication run of the Publish Docker workflow generates an SPDX SBOM of the published multi-architecture image and uploads it as a run artifact, and the Release SBOM workflow (`.github/workflows/release-sbom.yml`) generates the SBOM for a release's image tag and attaches it to that GitHub release when the release is published.

## Release watches

Two weekly workflows watch for upstream releases Print Handy cares about. Both are notification only: neither edits a pin or a baseline, neither opens a bump PR, and each opens at most one deduplicated issue per release, closing it again once Print Handy catches up.

- **Upstream watch** (`.github/workflows/upstream-watch.yml`, Mondays 13:37 UTC, `scripts/check_upstream_releases.py`): the bundled tools Print Handy pins manually (OrcaSlicer, go2rtc, Spoolman, Bambu Studio). It compares each pin with the upstream GitHub releases and reports under the `upstream-watch` label as `Upstream release: <Component> <version>`. An issue from it means a pin is behind; bumping the pin is a separate decision.
- **Firmware watch** (`.github/workflows/firmware-watch.yml`, Tuesdays 13:47 UTC, `scripts/check_firmware_releases.py`): printer firmware Print Handy audits but does not bundle. It compares each watched firmware's newest final upstream version against the **baseline** recorded in this repository (for ZMod, the audited-against version in [ZMOD.md](ZMOD.md); for everything else, `docs/firmware-baselines.toml`, where each entry names its evidence) and reports under the `firmware-watch` label as `Firmware release: <Firmware> <version>`. An issue from it means a release exists that Print Handy has not audited yet; the follow-up is a feature-by-feature audit in the pattern of #562 and #1269, and the issue closes when that audit moves the baseline. **A notice is not an audit**: it says the release exists, nothing about what the release changes for Print Handy, and the baseline never moves on its own.

What the firmware watch covers (#1271, #1280):

- **ZMod**, by GitHub releases, against the audit baseline in ZMOD.md.
- **Klipper and Moonraker**, by Git tags. Klipper has never published a GitHub release and Moonraker's releases stopped in 2021, so a releases check would stay silent forever; the script refuses one for those two repositories.
- **OctoPrint and Marlin**, by GitHub releases. Prereleases do not count, including OctoPrint's `2.0.0rc` line and Marlin betas published as full releases (`2.1.3-b3`). Marlin ships several maintenance lines at once; the watch follows the newest, so a backport to an older line opens no notice.
- **Prusa Buddy**, by GitHub releases, per printer line Print Handy supports (MK4S, CORE One, XL, MINI+). One repository carries divergent version lines, so a release belongs to a line when its title lists that printer by exact name. One release covering several lines opens one notice (`Firmware release: Prusa Buddy 8.0.0`) listing each line's baseline, and it closes once every line it covers has its baseline moved.
- **QIDI**, by GitHub releases, per model repository (Q1 Pro, X-Plus 3, X-Plus 4, X-Max 3, X-Smart 3), reading the version from the end of each model's tag (`Q1_Pro_V4.4.24`).

None of the non-ZMod families has had a feature-by-feature firmware audit like ZMod's. Their recorded baselines are the newest final upstream version when Print Handy's adapter and contract tests for that family were written (October 2026), so a notice means a release Print Handy's code has not been checked against. A feed whose entries the watch cannot read (no tag parses as a version, or no release title names a watched printer line) fails the run rather than reporting a quiet week. Stock Flashforge, Bambu Lab, Sovol, Elegoo, Creality and Anycubic firmware has no reliable machine-readable feed and is not watched; no page scraping fills the gap.

## Review cadence and exceptions

- Read the weekly report and update pins when a fixed upstream release exists. Bump the pin, its checksum or digest, and this table together.
- The `Slicer test (bambu)` CI leg runs a Flathub Bambu Studio build pinned by ostree commit (2.8.2.61 today), which does not move with `BAMBU_REF`. When a bump changes the Bambu generation (the first two version components), either pin a Flathub build of the new generation in `ci.yml` and in `tests/test_ci_bambu_workflow.py` (`FLATPAK_BAMBU_VERSION`, `FLATPAK_BAMBU_COMMIT`), or record a dated verdict for the new pair in that test's `RECORDED_DIVERGENCES`. The test fails until one of them is done.
- Re-check the cloudflared pin against the latest upstream release at least monthly, and after any cloudflared advisory. Look at the release notes for flag or behavior changes before moving the tag.
- Accepted findings (no fix available, or not reachable in this image) are recorded in the PR or issue that accepts them, with the component, identifier, reason and a date to re-review. Do not suppress findings in the workflow.
