"""Issue #117 release residue on top of main's Docker contract (test_docker_release_contract.py).

Settings parity: every user-adjustable setting in the schema has a docs/SETTINGS.md row and, when it
is a scalar, an .env.example line; every PRINT_HANDY__ name in a deployment file is a real setting.
Sanitization: public deployment files carry no LAN addresses, machine paths, IDs or credentials.
Build: OrcaSlicer is prebuilt-only (the one bundled build pinned by version, revision and SHA256);
bounded, validated parallelism remains only for the Bambu Studio source build. Unraid: the
single template carries only the minimal install surface; the runtime contract itself
lives in Compose, the mounted config file and the Web UI.
"""
import ipaddress
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from print_handy import printers, settings

ROOT = settings.ROOT
DOCS = (ROOT / "docs" / "SETTINGS.md").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
JOBS = ROOT / "docker" / "orca-build-jobs.sh"
UNRAID = ET.parse(ROOT / "docker" / "unraid" / "print-handy.xml").getroot()
# Public files a user copies or reads to deploy. All must be sanitized.
DEPLOYMENT_FILES = [".env.example", "docker-compose.yml", "Dockerfile",
                    "docker/entrypoint.sh", "docker/orca-build-jobs.sh", "docker/orca-prebuilt.sh", "docker/unraid/print-handy.xml",
                    "docker/supervise.sh", "docker/healthcheck.py", "config/docker.yaml", "config/docker.toml",
                    "docs/DOCKER.md", "docs/SETTINGS.md", "tests/docker/compose.acceptance.yml"]
SECRETS = {"DISCORD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "MATRIX_ACCESS_TOKEN", "LLM_API_KEY", "AGENT_API_KEY", "OCTOPRINT_API_KEY", "MOONRAKER_API_KEY", "CLIPROXY_API_KEY",
           "BAMBU_LAN_PASSWORD", "AD5X_SERIAL", "AD5X_CHECK_CODE", "WEBUI_ACCESS_TOKEN",
           "MQTT_PASSWORD", "NTFY_ACCESS_TOKEN",
           "DISCORD_CLIENT_SECRET", "LIVE_STREAM_SESSION_SECRET", "TUNNEL_TOKEN",
           "CREALITYCLOUD_TOKEN", "THANGS_TOKEN", "NEXPRINT_TOKEN",
           "MYMINIFACTORY_OAUTH_TOKEN", "MAKERWORLD_TOKEN", "THINGIVERSE_TOKEN", "CULTS3D_TOKEN"}
COMPOSE_VARS = {"PRINT_HANDY_IMAGE", "PRINT_HANDY_DATA", "PRINT_HANDY_SPOOLMAN_DATA", "PRINT_HANDY_CONFIG", "TZ",
                "DISCORD_CLIENT_ID", "CLOUDFLARED_IMAGE", "SPOOLMAN_CORS_ORIGIN"}
# NVIDIA container-runtime variables (#455): Unraid-template-only, optional,
# in Advanced View. Compose picks the GPU with a deploy block instead, and
# the image carries the NVIDIA_DRIVER_CAPABILITIES default, so they are not
# Compose vars.
UNRAID_GPU_VARS = {"NVIDIA_VISIBLE_DEVICES": "", "NVIDIA_DRIVER_CAPABILITIES": "compute,video,utility"}
RUNTIME_ENV_VARS = {"PRINT_HANDY_ORCA_CHANNEL", "PRINT_HANDY_USER_CONFIG", "PRINT_HANDY_BUNDLED_SPOOLMAN", "SPOOLMAN_HOST", "SPOOLMAN_UPDATE_CHECK", "PRINT_VISION_URL", "PRINT_FUZZY_URL"}
# The slimmed Unraid template's whole Config surface: the Web UI port, the
# two paths, the bundled/external Spoolman choice (advanced), TZ and the
# GPU entries. Everything else is set in the mounted config file, the
# Web UI, or the profile .env inside the Appdata path.
UNRAID_VARIABLES = {"PRINT_HANDY__SERVICES__SPOOLMAN_URL", "TZ"} | set(UNRAID_GPU_VARS)


def inventory():
    """{(section, key): example value} for every user-adjustable setting; open sections use key None."""
    home = settings._read(settings.HOME)
    found = {}
    for model in printers.MODELS.values():
        for section, values in settings._merge(model.defaults(), home).items():
            for key, value in values.items():
                found[(section, None if section in settings.OPEN_SECTIONS else key)] = value
    for (section, key), kind in settings.OPTIONAL_KEYS.items():
        found[(section, key)] = kind()
    return found


def env_name(section, key):
    return f"PRINT_HANDY__{section.upper()}__{key.upper()}"


def is_scalar(value):
    return not isinstance(value, (list, dict))


def table(heading):
    """{first column: second column} of the markdown table under a docs/SETTINGS.md heading."""
    text = DOCS.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    return dict(re.findall(r"^\| `([A-Z0-9_]+)` \| ([^|]+) \|", text, re.M))


class SettingsParityTests(unittest.TestCase):
    def test_migration_refuses_to_nest_existing_config_directory(self):
        script = """cd "$1"
if [ -e .config ]; then
  echo 'Both dot-config and .config exist. Compare and merge them before continuing.' >&2
  exit 1
fi
mv dot-config .config
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dot-config").mkdir()
            (root / "dot-config" / "bambu").mkdir()
            (root / ".config").mkdir()
            (root / ".config" / "print-handy").mkdir()
            result = subprocess.run(["bash", "-c", script, "migration", str(root)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "dot-config" / "bambu").is_dir())
            self.assertTrue((root / ".config" / "print-handy").is_dir())

    def setUp(self):
        self.inventory = inventory()
        self.rows = dict(re.findall(r"^\| `([a-z_]+\.(?:[a-z_]+|<[a-zA-Z]+>))` \| ([^|]+) \|", DOCS, re.M))

    def test_schema_includes_the_standalone_and_vendor_settings(self):
        for setting in [("bot", "mode"), ("bot", "health_port"), ("llm", "chat_url"), ("discord", "token_env"),
                        ("discord", "allowed_user_ids"), ("printer", "vendor"), ("printer", "slicer"),
                        ("delivery", "methods")]:
            self.assertIn(setting, self.inventory)

    def test_every_setting_has_a_reference_row_with_its_env_name(self):
        for (section, key), value in self.inventory.items():
            with self.subTest(setting=f"{section}.{key}"):
                if key is None:
                    self.assertTrue(any(name.startswith(f"{section}.<") for name in self.rows), section)
                    continue
                self.assertIn(f"{section}.{key}", self.rows)
                from_env = is_scalar(value) or (section, key) in settings.ENV_LISTS
                expected = f"`{env_name(section, key)}`" if from_env else "file only"
                self.assertEqual(self.rows[f"{section}.{key}"].strip(), expected)

    def test_every_reference_row_is_a_real_setting(self):
        for name in self.rows:
            section, key = name.split(".")
            with self.subTest(row=name):
                if section == "printers":
                    # The [[printers]] roster (#333) never appears in the defaults tree, so it
                    # is not in the inventory; its rows are pinned explicitly below instead.
                    continue
                self.assertIn((section, None if key.startswith("<") else key), self.inventory)

    def test_printers_roster_rows_document_every_entry_key(self):
        # [printers] is file-only and absent from the defaults tree, so the inventory walk
        # cannot see it; treat it explicitly (design §2.1): every entry key has a SETTINGS.md
        # row, and neither the docs nor .env.example spell a concrete indexed env name.
        for key in sorted(settings.PRINTER_ENTRY_KEYS):
            with self.subTest(entry_key=key):
                self.assertIn(f"printers.{key}", self.rows)
        self.assertNotIn("PRINT_HANDY__PRINTERS__", ENV_EXAMPLE)
        for name, env in self.rows.items():
            if name.startswith("printers."):
                self.assertNotRegex(env, r"PRINT_HANDY__PRINTERS__[A-Z0-9_]+__")

    def test_env_example_lists_every_scalar_setting(self):
        for (section, key), value in self.inventory.items():
            if key is not None and is_scalar(value):
                with self.subTest(setting=f"{section}.{key}"):
                    self.assertRegex(ENV_EXAMPLE, rf"(?m)^#? ?{env_name(section, key)}=")

    def test_every_documented_env_name_is_accepted_by_the_loader(self):
        names = set()
        for path in DEPLOYMENT_FILES:
            names |= set(re.findall(r"PRINT_HANDY__[A-Z0-9_]+__[A-Z0-9_]+", (ROOT / path).read_text()))
        names -= {"PRINT_HANDY__SECTION__KEY", "PRINT_HANDY__NOZZLE_TEMP_OFFSETS__TYPE"}
        self.assertIn("PRINT_HANDY__SERVICES__MOONRAKER_URL", names)
        for name in sorted(names):
            section, key = name[len("PRINT_HANDY__"):].lower().split("__")
            with self.subTest(env=name):
                self.assertTrue((section, key) in self.inventory or section in settings.OPEN_SECTIONS, name)

    def test_every_commented_example_value_loads_when_uncommented(self):
        """Integer spelling, booleans and URLs in .env.example are values the loader accepts."""
        environ = {}
        for line in ENV_EXAMPLE.splitlines():
            match = re.match(r"^#? ?(PRINT_HANDY__[A-Z0-9_]+)=(.+)$", line)
            # The model line, the delivery list and the ZMod delivery format are per-printer: the delivery
            # example is written for bambulab.a1 (tests/test_settings.py loads it there), the format only
            # applies to Flashforge/ZMod models, and docker.yaml selects klipper.generic.
            if match and "example" not in match.group(2) and match.group(1) not in ("PRINT_HANDY__PRINTER__MODEL",
                                                                                 "PRINT_HANDY__DELIVERY__METHODS",
                                                                                 "PRINT_HANDY__PRINTER__ZMOD_DELIVERY_FORMAT"):
                environ[match.group(1)] = match.group(2)
        self.assertGreater(len(environ), 40)
        data = settings.load(local=ROOT / "config" / "docker.yaml", environ=environ)
        self.assertEqual(data["bot"]["mode"], "standalone")

    def test_compose_build_and_secret_variables_are_documented(self):
        self.assertEqual(set(table("Compose variables")), COMPOSE_VARS)
        self.assertEqual(set(table("Runtime environment")), RUNTIME_ENV_VARS)
        self.assertEqual(set(table("Secrets")), SECRETS)
        args = set(re.findall(r"(?m)^ARG ([A-Z0-9_]+)", DOCKERFILE)) - {"TARGETARCH"}
        self.assertEqual(set(table("Build arguments")), args)
        for name, default in re.findall(r"(?m)^ARG ([A-Z0-9_]+)=(\S+)$", DOCKERFILE):
            with self.subTest(arg=name):
                self.assertEqual(table("Build arguments")[name].strip(), f"`{default}`")
        for name in SECRETS:
            with self.subTest(secret=name):
                self.assertRegex(ENV_EXAMPLE, rf"(?m)^#? ?{name}=$")


def _parse_dotted_quad(raw):
    """IP address for a dotted-quad match, or None when it is not an address.

    The scan regex also matches dotted version strings -- Bambu Studio's
    02.09.01.70 pin (PR #990) is the standing example -- whose leading
    zeros make ipaddress raise ValueError. Retry with the zeros stripped:
    a zero-padded real address (192.168.001.1) still parses and is checked
    below, while a string that is not an address even after stripping
    returns None and is skipped instead of crashing the scan.
    """
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        pass
    stripped = ".".join(str(int(octet)) for octet in raw.split("."))
    try:
        return ipaddress.ip_address(stripped)
    except ValueError:
        return None


class SanitizedExamplesTests(unittest.TestCase):
    def test_dotted_quad_helper_distinguishes_versions_from_addresses(self):
        # Direct pins for _parse_dotted_quad (landed with PR #990): the
        # deployment-file scan below proves the version tolerance against
        # the real files, but the positive direction — a real LAN
        # address, including a zero-padded one, still parses and would
        # be flagged — was asserted nowhere.
        assert _parse_dotted_quad("192.168.250.135").is_private
        assert _parse_dotted_quad("192.168.001.1").is_private
        # The Bambu version string zero-strips to a valid PUBLIC address
        # (2.9.1.70), so the scan parses it and does not flag it — that,
        # not a parse failure, is what unblocked PR #990.
        version = _parse_dotted_quad("02.09.01.70")
        assert str(version) == "2.9.1.70" and not version.is_private
        assert not _parse_dotted_quad("8.8.8.8").is_private

    def test_no_lan_addresses_machine_paths_ids_or_credentials(self):
        for path in DEPLOYMENT_FILES:
            text = (ROOT / path).read_text()
            with self.subTest(file=path):
                for raw in re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", text):
                    address = _parse_dotted_quad(raw)
                    if address is None:
                        # Not an IP address (a version string, above).
                        continue
                    # The unspecified address (0.0.0.0) is a bind wildcard for
                    # the optional Web UI (#286), not a machine's LAN address.
                    self.assertFalse(address.is_private and not address.is_loopback
                                     and not address.is_unspecified, f"{path}: LAN address {raw}")
                self.assertIsNone(re.search(r"/(home|Users)/(?!app/)[a-z]", text), f"{path}: machine path")
                self.assertIsNone(re.search(r"/mnt/user/(?!appdata/)", text), f"{path}: personal share")
                self.assertIsNone(re.search(r"(?<![0-9a-f])(?!100000000000000001|200000000000000002)\d{17,20}(?![0-9a-f])",
                                            text), f"{path}: Discord ID")
                self.assertIsNone(re.search(r"[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}|gh[pousr]_[A-Za-z0-9]{20,}"
                                            r"|sk-[A-Za-z0-9]{20,}", text), f"{path}: credential")
                for host in re.findall(r"https?://([A-Za-z0-9.-]+)", text):
                    self.assertFalse(host.endswith((".lan", ".local", ".home", ".internal")), f"{path}: private host {host}")

    def test_required_secrets_are_empty_in_the_example(self):
        for line in ENV_EXAMPLE.splitlines():
            name = line.lstrip("# ").split("=", 1)[0]
            if name in SECRETS:
                with self.subTest(secret=name):
                    self.assertEqual(line.split("=", 1)[1], "")


class OrcaBuildJobsTests(unittest.TestCase):
    """docker/orca-build-jobs.sh: bounded, memory-aware, validated."""

    def run_jobs(self, request, nproc=8, mem_kib=None, cpu_max=None, memory_max=None, per_job=None):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root, "proc")
            proc.mkdir()
            (proc / "meminfo").write_text(f"MemTotal: 99999999 kB\nMemAvailable: {mem_kib or 64 * 1024 * 1024} kB\n")
            cgroup = Path(root, "sys", "fs", "cgroup")
            cgroup.mkdir(parents=True)
            if cpu_max:
                (cgroup / "cpu.max").write_text(cpu_max + "\n")
            if memory_max:
                (cgroup / "memory.max").write_text(memory_max + "\n")
            env = {"PATH": os.environ["PATH"], "ORCA_JOBS_ROOT": root, "ORCA_JOBS_NPROC": str(nproc)}
            if per_job:
                env["ORCA_BUILD_MEM_PER_JOB_MIB"] = str(per_job)
            return subprocess.run(["sh", str(JOBS), request], capture_output=True, text=True, env=env, timeout=30)

    def jobs(self, *args, **kwargs):
        result = self.run_jobs(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout)

    def test_auto_never_takes_every_cpu(self):
        self.assertEqual(self.jobs("auto", nproc=8), 7)
        self.assertEqual(self.jobs("auto", nproc=1), 1)

    def test_auto_is_bounded_by_memory(self):
        six_gib = 6 * 1024 * 1024
        self.assertEqual(self.jobs("auto", nproc=4, mem_kib=six_gib), 2)
        self.assertEqual(self.jobs("auto", nproc=4, memory_max=str(6 * 1024 ** 3)), 2)
        self.assertEqual(self.jobs("auto", nproc=4, mem_kib=1024 * 1024), 1)
        self.assertEqual(self.jobs("auto", nproc=16, mem_kib=six_gib, per_job=1024), 6)

    def test_auto_respects_a_cgroup_cpu_quota(self):
        self.assertEqual(self.jobs("auto", nproc=16, cpu_max="400000 100000"), 3)
        self.assertEqual(self.jobs("auto", nproc=4, cpu_max="max 100000"), 3)

    def test_explicit_override_and_validation(self):
        self.assertEqual(self.jobs("5", nproc=2, mem_kib=1024), 5)
        for bad in ("0", "65", "-1", "two", "", "1.5", "08"):
            with self.subTest(request=bad):
                result = self.run_jobs(bad)
                self.assertEqual(result.returncode, 2)
                self.assertIn("ORCA_BUILD_JOBS", result.stderr)
        self.assertEqual(self.run_jobs("auto", per_job=100).returncode, 2)

    def test_jobs_apply_to_the_bambu_dependencies_and_application(self):
        build = DOCKERFILE.split(" AS runtime")[0]
        self.assertIn('jobs="$(orca-build-jobs "$ORCA_BUILD_JOBS")"', build)      # Bambu Studio source build only
        self.assertIn('export CMAKE_BUILD_PARALLEL_LEVEL="$jobs"', build)       # every dependency's own build
        self.assertIn("cmake --build deps/build --parallel 1", build)             # one dependency at a time
        self.assertIn('cmake --build build --parallel "$jobs"', build)            # the application
        self.assertNotIn("$(nproc)", build)


class OrcaPrebuiltTests(unittest.TestCase):
    """OrcaSlicer is never built from source: the one bundled build is a pinned official prebuilt."""

    def test_no_orca_source_build_remains(self):
        self.assertFalse((ROOT / "docker" / "orca-build.sh").exists())
        for marker in ("ORCA_SOURCE", "ORCA_STABLE_REF", "ORCA_ALPHA_REF", "ORCA_BUILD_PROFILE",
                       "AS orca-base", "AS orca-stable", "AS orca-alpha", "print-handy-orca-"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, DOCKERFILE)
        # The only remaining build cache is the Bambu Studio one.
        mounts = re.findall(r"--mount=type=cache,id=(\S+?),target=/cache,sharing=locked", DOCKERFILE)
        self.assertEqual(len(mounts), 1)
        self.assertIn("print-handy-bambu-", mounts[0])
        self.assertIn('COPY --from=orca-prebuilt /out/ /', DOCKERFILE)
        self.assertIn('sh /usr/local/bin/orca-prebuilt stable "$TARGETARCH"', DOCKERFILE)
        self.assertNotIn('orca-prebuilt preview', DOCKERFILE)

    def test_the_single_channel_is_a_pinned_prebuilt(self):
        helper = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        blocks = dict(re.findall(r"(stable)\)\n(.*?)\n        ;;", helper, re.S))
        # The one bundled build pins OrcaSlicer v2.5.0-rc (the successor of
        # the v2.5.0-beta pinned before the single-Orca change; the beta pin
        # and the preview block are gone).
        for pin in ("version=2.5.0", "revision=40ecc66f78ed72dc368e0c1ebe40d77f00cc8a19",
                    "sha256_amd64=d13eb0a9cd618ed080ab6b82c64c0a60c4501aac91e88d5ac2c3b526fb141a7c",
                    "asset_amd64=OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage",
                    "sha256_arm64=00a4fa0a2f9c1649abb8c438b0537ed1e58fed5206faa560c1c0fb4ab225f9cf",
                    "asset_arm64=OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0.AppImage"):
            with self.subTest(channel="stable", pin=pin):
                self.assertIn(pin, blocks["stable"])
        self.assertNotIn("2.5.0-beta", helper)
        self.assertNotIn("preview)", helper)
        self.assertIn("set -eu", helper)
        self.assertIn("sha256sum --check --strict -", helper)
        self.assertLess(helper.index("sha256sum --check"), helper.index("--appimage-extract"))
        self.assertIn("--appimage-extract", helper)
        self.assertNotIn("--appimage-extract-and-run", helper)
        self.assertNotIn("cmake", helper)

    def test_per_arch_pins_parse_to_the_official_assets_and_digests(self):
        """The script's pin table, parsed not eyeballed: arch -> asset + SHA256.

        The revision is shared by both architectures; the aarch64 asset
        digest is the upstream GitHub release asset digest for the pinned
        version, matching the amd64 pin's provenance.
        """
        helper = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        blocks = dict(re.findall(r"(stable)\)\n(.*?)\n        ;;", helper, re.S))
        self.assertEqual(set(blocks), {"stable"})
        expected = {
            "stable": ("2.5.0", "40ecc66f78ed72dc368e0c1ebe40d77f00cc8a19", {
                "amd64": ("OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage",
                          "d13eb0a9cd618ed080ab6b82c64c0a60c4501aac91e88d5ac2c3b526fb141a7c"),
                "arm64": ("OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0.AppImage",
                          "00a4fa0a2f9c1649abb8c438b0537ed1e58fed5206faa560c1c0fb4ab225f9cf"),
            }),
        }
        for channel, (version, revision, arches) in expected.items():
            with self.subTest(channel=channel):
                block = blocks[channel]
                self.assertIn(f"version={version}", block)
                self.assertIn(f"revision={revision}", block)
                for arch, (asset, sha256) in arches.items():
                    self.assertIn(f"asset_{arch}={asset}", block)
                    self.assertIn(f"sha256_{arch}={sha256}", block)
        # Selection maps the Docker arch onto its pin pair at runtime.
        self.assertIn("amd64) sha256=$sha256_amd64; asset=$asset_amd64 ;;", helper)
        self.assertIn("arm64) sha256=$sha256_arm64; asset=$asset_arm64 ;;", helper)

    def test_prebuilt_accepts_amd64_and_arm64_and_refuses_other_arches(self):
        helper = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        self.assertIn("amd64|arm64) ;;", helper)
        self.assertNotIn("amd64 only", helper)
        # Build-info records the real target arch, never a hardcoded one.
        self.assertIn("arch=%s", helper)
        self.assertIn('"$arch" "$sha256"', helper)
        self.assertNotIn("arch=amd64", helper)
        script = ROOT / "docker" / "orca-prebuilt.sh"
        for bad_arch in ("arm", "arm/v7", "v7", "386", "ppc64le", ""):
            with self.subTest(arch=bad_arch):
                result = subprocess.run(["sh", str(script), "stable", bad_arch],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn("amd64 and arm64 only", result.stderr)
        # A supported arch with an unknown channel is refused before any download.
        # "preview" and "alpha" are refused here too: they are only runtime
        # entrypoint aliases for the single build, never build channels.
        for arch in ("amd64", "arm64"):
            for bad_channel in ("beta", "alpha", "preview"):
                with self.subTest(arch=arch, channel=bad_channel):
                    result = subprocess.run(["sh", str(script), bad_channel, arch],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("usage: orca-prebuilt.sh stable amd64|arm64", result.stderr)

    def test_the_single_build_has_its_wrapper_and_identity(self):
        helper = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        self.assertIn('mv "squashfs-root" "/out/opt/orca-$channel"', helper)
        self.assertIn('"/out/usr/local/bin/orca-slicer-$channel"', helper)
        self.assertIn("exec /opt/orca-%s/AppRun", helper)
        self.assertIn('> "/out/orca-$channel-build-info"', helper)
        self.assertIn('> "/out/orca-$channel-source-revision"', helper)
        # Only stable gets the default /usr/local/bin/orca-slicer symlink.
        self.assertIn('if [ "$channel" = stable ]; then', helper)
        self.assertIn("ln -s orca-slicer-stable /out/usr/local/bin/orca-slicer", helper)


class RuntimeImageTests(unittest.TestCase):
    def test_every_stage_uses_debian_stable(self):
        """Debian 13 slim for every stage (Orca, go2rtc, Bambu, Spoolman, bounds, runtime); no stock UID 1000 user, so handy takes 1000."""
        self.assertEqual(re.findall(r"^FROM (debian:13-slim)@sha256:[0-9a-f]{64}(?: AS [\w-]+)?$", DOCKERFILE, re.M),
                         ["debian:13-slim"] * 6)
        runtime = DOCKERFILE.split(" AS runtime", 1)[1]
        self.assertIn("useradd --uid 1000 --create-home handy", runtime)
        self.assertNotIn("ubuntu", DOCKERFILE)

    def test_every_base_image_is_pinned_by_one_digest(self):
        """#1196: a tag moves, a digest does not. Every stage, the BuildKit
        syntax frontend and the docker-static job container name their image
        by digest (tag kept for reading), and the six Debian stages and the
        job container share one digest, so a base bump is one change that
        Renovate's dockerfile and github-actions managers make everywhere."""
        digests = set(re.findall(r"^FROM debian:13-slim@(sha256:[0-9a-f]{64})", DOCKERFILE, re.M))
        self.assertEqual(len(digests), 1, digests)
        self.assertEqual(len(re.findall(r"^FROM ", DOCKERFILE, re.M)), 6)
        self.assertRegex(DOCKERFILE.splitlines()[0],
                         r"^# syntax=docker/dockerfile:1@sha256:[0-9a-f]{64}$")
        static = (ROOT / ".github" / "workflows" / "docker-static.yml").read_text()
        container = re.findall(r"^\s+image: (debian:13-slim@sha256:[0-9a-f]{64})$", static, re.M)
        self.assertEqual(container, [f"debian:13-slim@{digests.copy().pop()}"])


class NativeBoundsStageTests(unittest.TestCase):
    """The bounds-build stage (#952): maturin only places the extension
    at print_handy/core/_bounds_native when the package skeleton is
    staged beside the crate; without those copies the wheel silently
    ships a top-level package the dispatcher can never import."""

    def test_stage_stages_the_package_skeleton_and_ships_only_the_wheel(self):
        stage = DOCKERFILE.split(" AS bounds-build", 1)[1].split(" AS runtime", 1)[0]
        self.assertIn("COPY print_handy/__init__.py ./native/bounds/print_handy/__init__.py", stage)
        self.assertIn("COPY print_handy/core/__init__.py ./native/bounds/print_handy/core/__init__.py", stage)
        self.assertIn("maturin build --release", stage)
        runtime = DOCKERFILE.split(" AS runtime", 1)[1]
        # Only the .so leaves the wheel (never its staged __init__ files).
        self.assertNotIn("pip install --no-cache-dir /tmp/bounds-wheel/*.whl", runtime)
        self.assertIn("python -m zipfile -e *.whl x", runtime)
        self.assertIn('cp "$so" "$site/print_handy/core/" && cp "$so" /app/print_handy/core/', runtime)
        # The staged pip install above the copy step (pyproject.toml +
        # print_handy/__init__.py only, for dependency-layer caching)
        # leaves the site-packages package without a core/ directory, and
        # cp does not create it: the step must mkdir it, before the copy,
        # or the image build dies at this step on both architectures
        # (develop publish run 42410316669).
        mkdir_at = runtime.index('mkdir -p "$site/print_handy/core"')
        cp_at = runtime.index('cp "$so" "$site/print_handy/core/"')
        self.assertLess(mkdir_at, cp_at)
        # The placed /app copy is import-checked inside the stage itself,
        # so a placement the loader cannot use fails the build, not the
        # first publish that happens to exercise it.
        self.assertIn('python -c "from print_handy.core import _bounds_native"', runtime)
        # The rustup installer is checksum-pinned per architecture, never piped to sh.
        self.assertNotIn("sh.rustup.rs", stage)
        self.assertIn("rustup/archive/1.28.2/$target/rustup-init", stage)
        self.assertEqual(stage.count("sha256sum -c -"), 1)
        self.assertNotIn("cargo", runtime)
        self.assertNotIn("rustup", runtime)


class OrtFlavorStageTests(unittest.TestCase):
    """GPU inference flavors for the local detector (#455): ORT_FLAVOR
    swaps the runtime stage's ONNX Runtime distribution after the plain
    install; the published image builds with the default (cpu), which
    must stay a literal no-op."""

    def runtime(self):
        return DOCKERFILE.split(" AS runtime", 1)[1]

    def test_flavor_arg_defaults_to_cpu_in_the_runtime_stage(self):
        self.assertIn("ARG ORT_FLAVOR=cpu", self.runtime())

    def test_cpu_branch_is_a_noop_and_unknown_flavors_refuse(self):
        runtime = self.runtime()
        self.assertIn("cpu) ;;", runtime)
        self.assertIn("*) echo 'ORT_FLAVOR must be cpu, openvino or cuda' >&2; exit 2 ;;", runtime)
        # The flavor step runs after the plain install it modifies.
        self.assertLess(runtime.index("pip install --no-cache-dir ."),
                        runtime.index('case "$ORT_FLAVOR" in'))

    def test_openvino_flavor_is_amd64_only_and_pinned(self):
        runtime = self.runtime()
        self.assertIn('if [ "$TARGETARCH" != "amd64" ]', runtime)
        self.assertIn("onnxruntime-openvino publishes no arm64 wheel", runtime)
        self.assertIn('pip install --no-cache-dir "onnxruntime-openvino==1.24.1"', runtime)
        self.assertIn("libonnxruntime_providers_openvino.so", runtime)

    def test_cuda_flavor_is_pinned_with_its_runtime_extras(self):
        runtime = self.runtime()
        self.assertIn('pip install --no-cache-dir "onnxruntime-gpu[cuda,cudnn]==1.31.1"', runtime)
        self.assertIn("libonnxruntime_providers_cuda.so", runtime)

    def test_flavors_swap_the_distribution_the_plain_install_brought(self):
        runtime = self.runtime()
        # Both flavors uninstall the CPU distribution before installing
        # theirs — the two distributions ship the same module and must
        # never coexist.
        self.assertEqual(runtime.count("pip uninstall -y onnxruntime"), 2)


class ContainerTimezoneTests(unittest.TestCase):
    """TZ: the image carries a default container timezone (America/New_York)
    that Compose, .env.example, the Unraid template and the docs all expose,
    and the entrypoint keeps a valid override while falling back on an
    unknown name."""

    def test_image_installs_the_tz_database_and_defaults_tz(self):
        runtime = DOCKERFILE.split(" AS runtime", 1)[1]
        self.assertIn("tzdata", runtime)
        self.assertIn("TZ=America/New_York", runtime)
        self.assertIn('ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime', runtime)

    def test_compose_passes_tz_with_the_default(self):
        env = COMPOSE["services"]["combined"]["environment"]
        self.assertEqual(env["TZ"], "${TZ:-America/New_York}")

    def test_env_example_and_settings_carry_the_default(self):
        self.assertRegex(ENV_EXAMPLE, r"(?m)^TZ=America/New_York$")
        self.assertEqual(table("Compose variables")["TZ"].strip(), "`America/New_York`")

    def test_unraid_template_defaults_tz(self):
        variables = {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}
        self.assertEqual(variables["TZ"].get("Default"), "America/New_York")
        self.assertEqual(variables["TZ"].text, "America/New_York")

    # #971: runs the shipped image's entrypoint, so it is container-marked
    # (runs in the image and in the CI offline-tests job; skips locally).
    @pytest.mark.container
    def test_entrypoint_keeps_a_valid_tz_and_falls_back_on_an_unknown_one(self):
        script = ROOT / "docker" / "entrypoint.sh"
        with tempfile.TemporaryDirectory(dir=ROOT / ".") as data:
            command = ["sh", "-c", "printf \"%s\\n\" \"$TZ\""]
            base = {"PATH": os.environ["PATH"], "HOME": data}
            valid = subprocess.run(["bash", str(script), *command], cwd=data,
                                   env={**base, "TZ": "Europe/London"},
                                   text=True, capture_output=True, check=True)
            self.assertEqual(valid.stdout.strip(), "Europe/London")
            invalid = subprocess.run(["bash", str(script), *command], cwd=data,
                                     env={**base, "TZ": "Not/AZone"},
                                     text=True, capture_output=True, check=True)
            self.assertEqual(invalid.stdout.strip(), "America/New_York")
            self.assertIn("not a known IANA timezone", invalid.stderr)


    def test_migration_refuses_to_nest_existing_config_directory(self):
        script = """cd "$1"
if [ -e .config ]; then
  echo 'Both dot-config and .config exist. Compare and merge them before continuing.' >&2
  exit 1
fi
mv dot-config .config
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dot-config").mkdir()
            (root / "dot-config" / "bambu").mkdir()
            (root / ".config").mkdir()
            (root / ".config" / "print-handy").mkdir()
            result = subprocess.run(["bash", "-c", script, "migration", str(root)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "dot-config" / "bambu").is_dir())
            self.assertTrue((root / ".config" / "print-handy").is_dir())

    def configs(self, root, kind):
        return {c.get("Target"): c for c in root.findall("Config") if c.get("Type") == kind}

    def setUp(self):
        self.service = COMPOSE["services"]["combined"]
        params = shlex.split(UNRAID.findtext("ExtraParams") or "")
        self.params = params
        self.flags = {flag: params[i + 1] for i, flag in enumerate(params[:-1]) if flag.startswith("--")}

    def test_template_runs_the_combined_compose_service(self):
        # The combined service is the template's container; the only other
        # Compose services are the one-shot first-boot init service (#1144)
        # and the optional cloudflared sidecar (#311), which
        # is profile-gated, shares the combined network namespace and is a
        # separate Unraid container there — never part of this template.
        self.assertEqual(list(COMPOSE["services"]), ["combined", "print-handy-init", "cloudflared"])
        sidecar = COMPOSE["services"]["cloudflared"]
        self.assertEqual(sidecar["profiles"], ["live-stream"])
        self.assertEqual(sidecar["network_mode"], "service:combined")
        self.assertEqual(UNRAID.findtext("Name"), "print-handy")
        self.assertFalse(UNRAID.findtext("PostArgs"))
        self.assertNotIn("command", self.service)
        # The template's only docker-run flag is --shm-size, equal to
        # Compose's shm_size; the healthcheck is the Compose service's
        # own, not a template --health-cmd flag.
        self.assertEqual(self.params, [f"--shm-size={self.service['shm_size']}"])
        self.assertEqual(self.service["healthcheck"]["test"],
                         ["CMD", "python3", "/usr/local/bin/print-handy-healthcheck"])
        publish = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()
        image = re.search(r"(?m)^      IMAGE: (ghcr\.io/[^\s]+)$", publish)
        if image is None:
            self.fail("docker-publish.yml has no IMAGE")
        image_name = image.group(1)
        self.assertIn("docker buildx imagetools create -t \"$IMAGE:latest\"", publish)
        # The publisher still moves :latest. The template must not: DockerMan
        # treats Repository as the image to pull, and a moving tag is how an
        # install drifts. Pin the multi-arch index digest (name@sha256:...).
        repository = UNRAID.findtext("Repository") or ""
        self.assertTrue(repository.startswith(f"{image_name}@sha256:"))
        self.assertRegex(repository, r"@sha256:[0-9a-f]{64}$")
        self.assertNotIn(":latest", repository)
        self.assertNotIn(":develop", repository)
        self.assertNotIn(":nightly", repository)
        self.assertEqual(UNRAID.findtext("Privileged"), "false")
        # Compose publishes exactly one port by default: the Web UI's
        # 8088, so a first boot is reachable from the host. The bundled
        # Spoolman API stays on loopback by default (its mapping below is
        # a commented example only), and the slimmed template's only Port
        # config is the Web UI's (#286 stage 5). The #443 Spoolman LAN
        # mapping is one an operator adds by hand; it answers nothing
        # while SPOOLMAN_HOST keeps its 127.0.0.1 default (see
        # SpoolmanLanTests).
        self.assertEqual(self.service["ports"],
                         ["8088:${PRINT_HANDY__WEBUI__PORT:-8088}"])
        self.assertNotIn("-p", self.params)
        self.assertFalse(any(p.startswith("--publish") for p in self.params))
        ports = self.configs(UNRAID, "Port")
        self.assertEqual(set(ports), {"8088"})
        self.assertEqual(ports["8088"].text, "8088")
        self.assertEqual(UNRAID.findtext("WebUI"), "http://[IP]:[PORT:8088]/")

    def test_hardening_and_limits_live_in_compose_not_the_template(self):
        service = self.service
        # The one docker-run flag the template adds is --shm-size, and it
        # must equal Compose's value: Unraid's Docker default of 64 MB
        # starves OrcaSlicer under xvfb. Nothing else in ExtraParams, so
        # nothing else can drift from the runtime contract.
        self.assertEqual(self.params, ["--shm-size=256m"])
        self.assertEqual(service["shm_size"], "256m")
        # The hardening the flags used to duplicate stays in Compose, where
        # it is expressed as service keys instead of CLI flags.
        self.assertEqual(service["user"], "1000:1000")
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(service["shm_size"], "256m")
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertEqual(service["logging"]["driver"], "json-file")
        self.assertEqual(service["logging"]["options"], {"max-size": "10m", "max-file": "3"})
        for key, expected in (("interval", "30s"), ("timeout", "10s"),
                              ("start_period", "90s"), ("retries", 3)):
            with self.subTest(healthcheck=key):
                self.assertEqual(service["healthcheck"][key], expected)
        # No resource limits ship: a pids/memory/cpus limit starved OrcaSlicer on a large host.
        for flag in ("--pids-limit", "--memory", "--cpus"):
            self.assertNotIn(flag, self.flags)
        for key in ("pids_limit", "mem_limit", "cpus", "deploy"):
            self.assertNotIn(key, service)

    def test_template_carries_only_the_install_surface(self):
        """The slimmed template (install-surface slimming, 2026-11-08): the
        Web UI port, one Appdata path for all of /data, the Timelapse
        Archive path, the bundled/external Spoolman choice in Advanced
        View, TZ and the GPU entries — and nothing else. Every setting
        the old template carried is set in the mounted config file, the
        Web UI, or the profile .env inside the Appdata path; every secret
        arrives that way too (no masked template fields remain)."""
        compose_env = self.service["environment"]
        paths = self.configs(UNRAID, "Path")
        self.assertEqual(set(paths), {"/data", "/data/timelapses"})
        self.assertEqual(paths["/data"].text, "/mnt/user/appdata/print-handy")
        self.assertEqual(paths["/data"].get("Required"), "true")
        self.assertEqual(paths["/data/timelapses"].text,
                         "/mnt/user/appdata/print-handy/timelapses")
        self.assertEqual(paths["/data/timelapses"].get("Required"), "false")
        self.assertEqual(UNRAID.findtext("Category"), "Tools:Utilities Productivity:")
        variables = self.configs(UNRAID, "Variable")
        self.assertEqual(set(variables), UNRAID_VARIABLES)
        self.assertEqual(set(self.configs(UNRAID, "Port")), {"8088"})
        self.assertEqual(set(self.configs(UNRAID, "Device")), {"/dev/dri"})
        # The one surviving settings variable: the bundled/external
        # Spoolman choice, in Advanced View, still agreeing with Compose.
        spoolman_url = variables["PRINT_HANDY__SERVICES__SPOOLMAN_URL"]
        self.assertEqual(spoolman_url.get("Display"), "advanced")
        self.assertEqual(spoolman_url.text,
                         str(compose_env["PRINT_HANDY__SERVICES__SPOOLMAN_URL"]).split(":-")[-1].rstrip("}"))
        section, key = "services", "spoolman_url"
        self.assertIn((section, key), inventory())
        # No secret is a template field anymore, and nothing the template
        # still names is masked.
        for target, config in variables.items():
            with self.subTest(variable=target):
                self.assertNotIn(target, SECRETS)
                self.assertEqual(config.get("Mask"), "false")
        # The runtime Orca selector (#180) stays a Compose/.env surface:
        # the template no longer carries it.
        self.assertEqual(compose_env["PRINT_HANDY_ORCA_CHANNEL"], "${PRINT_HANDY_ORCA_CHANNEL:-stable}")
        self.assertRegex(ENV_EXAMPLE, r"(?m)^PRINT_HANDY_ORCA_CHANNEL=stable$")

    def test_dockerfile_volume_paths_are_mounted_exactly_by_every_shipped_deployment(self):
        """A Dockerfile VOLUME makes Docker create an anonymous volume for each
        declared path a run does not mount exactly, so a template that binds
        only the parent /data silently sends that data to hidden volumes
        (confirmed with a real docker create). Being "under /data" is not
        persistence. Every VOLUME path must be a target that Compose and the
        Unraid template both mount exactly; the image declares none today."""
        declared = []
        for match in re.finditer(r"(?m)^VOLUME\s+(.+)$", DOCKERFILE):
            raw = match.group(1).strip()
            declared += json.loads(raw) if raw.startswith("[") else raw.split()
        compose_targets = {v.split(":")[1] for v in self.service["volumes"]}
        unraid_targets = set(self.configs(UNRAID, "Path"))
        for path in declared:
            with self.subTest(volume=path):
                self.assertIn(path, compose_targets)
                self.assertIn(path, unraid_targets)

    def test_unraid_gpu_entries_are_optional_and_in_advanced_view(self):
        """#455: the GPU passthrough entries survive the template slimming
        (they are install-time surface — a device grant cannot be a config
        setting), but they are optional, sit in Advanced View, grant no
        GPU by default, and keep the "video" capability NVENC needs (the
        image default). The /dev/dri device entry is the Intel/AMD half;
        the NVIDIA runtime flag itself stays an operator Extra Parameter:
        ExtraParams ships empty."""
        variables = self.configs(UNRAID, "Variable")
        dockerfile = (ROOT / "Dockerfile").read_text()
        for target, default in UNRAID_GPU_VARS.items():
            with self.subTest(variable=target):
                config = variables[target]
                self.assertEqual(config.get("Required"), "false")
                self.assertEqual(config.get("Display"), "advanced")
                self.assertEqual(config.get("Mask"), "false")
                self.assertEqual(config.get("Default"), default)
                self.assertEqual(config.text or "", default)
        self.assertIn("--runtime=nvidia",
                      variables["NVIDIA_VISIBLE_DEVICES"].get("Description"))
        devices = self.configs(UNRAID, "Device")
        self.assertEqual(devices["/dev/dri"].get("Required"), "false")
        self.assertEqual(devices["/dev/dri"].get("Display"), "advanced")
        # Default names the device, the value is empty: a host without an
        # iGPU has no /dev/dri and docker run fails when it is passed.
        self.assertEqual(devices["/dev/dri"].get("Default"), "/dev/dri")
        self.assertFalse((devices["/dev/dri"].text or "").strip())
        self.assertIn("render group", devices["/dev/dri"].get("Description"))
        self.assertIn("video", UNRAID_GPU_VARS["NVIDIA_DRIVER_CAPABILITIES"].split(","))
        self.assertIn(f"ENV NVIDIA_DRIVER_CAPABILITIES={UNRAID_GPU_VARS['NVIDIA_DRIVER_CAPABILITIES']}",
                      dockerfile)
        self.assertNotIn("--runtime", UNRAID.findtext("ExtraParams") or "")
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", self.service.get("environment", {}))


class UnraidCaTemplateParityTests(unittest.TestCase):
    """#1143: the in-repo Unraid template and the Community Applications
    copy in catduckgnaf/catduck-unraid-templates are the same install
    surface, so every field they share must agree exactly, and the
    fields that deliberately differ must differ in exactly the
    documented ways. CI cannot read the CA repository from the network,
    so a byte-exact snapshot of the CA template is checked in at
    tests/fixtures/unraid/print-handy-ca.xml; its refresh path and the
    intentional differences are documented in
    tests/fixtures/unraid/README.md. Refreshing the snapshot is a
    deliberate, reviewed act: any drift among the snapshot, the in-repo
    template and the pins below fails here instead of being absorbed.
    """

    CA = ET.parse(ROOT / "tests" / "fixtures" / "unraid" / "print-handy-ca.xml").getroot()

    @staticmethod
    def configs_by_key(root):
        return {(c.get("Type"), c.get("Target")): c for c in root.findall("Config")}

    def test_shared_top_level_fields_match(self):
        for tag in ("Name", "Network", "MyIP", "Shell", "Privileged",
                    "Project", "Category", "WebUI", "ExtraParams",
                    "PostArgs", "CPUset", "DonateText", "DonateLink"):
            with self.subTest(field=tag):
                self.assertEqual(UNRAID.findtext(tag) or "",
                                 self.CA.findtext(tag) or "")

    def test_overview_matches_apart_from_the_docs_link_sentence(self):
        docs_sentence = re.compile(
            r"Full setup and upgrade instructions are in the Print Handy docs"
            r"(?:\.|: https://\S+? \(Docker setup guide: https://\S+\)\.)")

        def normalized(root):
            text = docs_sentence.sub("", root.findtext("Overview") or "")
            return re.sub(r"\s+", " ", text).strip()

        self.assertEqual(normalized(UNRAID), normalized(self.CA))
        # Both still carry the setup facts #1143 requires of the Overview:
        # the ownership command and the bot token location.
        for root in (UNRAID, self.CA):
            overview = root.findtext("Overview") or ""
            self.assertIn("chown -R 1000:1000 /mnt/user/appdata/print-handy", overview)
            self.assertIn("state/.env", overview)

    def test_shared_config_entries_match_apart_from_two_descriptions(self):
        in_repo = self.configs_by_key(UNRAID)
        ca = self.configs_by_key(self.CA)
        differing_descriptions = {("Path", "/data"), ("Device", "/dev/dri")}
        for key in sorted(set(in_repo) & set(ca)):
            with self.subTest(config=key):
                for attribute in ("Name", "Target", "Default", "Mode",
                                  "Type", "Display", "Required", "Mask"):
                    self.assertEqual(in_repo[key].get(attribute),
                                     ca[key].get(attribute))
                self.assertEqual(in_repo[key].text or "", ca[key].text or "")
                if key not in differing_descriptions:
                    self.assertEqual(in_repo[key].get("Description"),
                                     ca[key].get("Description"))

    def test_the_two_differing_descriptions_keep_their_safety_core(self):
        in_repo = self.configs_by_key(UNRAID)
        ca = self.configs_by_key(self.CA)
        for key in (("Path", "/data"), ("Device", "/dev/dri")):
            with self.subTest(config=key):
                self.assertNotEqual(in_repo[key].get("Description"),
                                    ca[key].get("Description"))
        for configs in (in_repo, ca):
            self.assertIn("chown -R 1000:1000 /mnt/user/appdata/print-handy",
                          configs[("Path", "/data")].get("Description"))
            device = configs[("Device", "/dev/dri")].get("Description")
            self.assertIn("ships empty", device)
            self.assertIn("render group", device)

    def test_ca_only_paths_are_the_former_volume_paths_plus_config(self):
        in_repo = self.configs_by_key(UNRAID)
        ca = self.configs_by_key(self.CA)
        ca_only = {target for (kind, target) in set(ca) - set(in_repo) if kind == "Path"}
        self.assertEqual(ca_only, {"/data/state", "/data/hermes", "/data/archive",
                                   "/data/records", "/data/cache", "/data/spoolman",
                                   "/data/.config"})
        # Each CA-only path is one of the folders the image used to
        # declare as a VOLUME (plus .config), mapped to the matching
        # folder under the same Appdata path the in-repo template uses,
        # so an install created on a pre-fix image keeps its data.
        required = {"/data/state": "true", "/data/hermes": "false",
                    "/data/archive": "true", "/data/records": "true",
                    "/data/cache": "true", "/data/spoolman": "true",
                    "/data/.config": "false"}
        for target, expected_required in required.items():
            with self.subTest(path=target):
                config = ca[("Path", target)]
                host = f"/mnt/user/appdata/print-handy{target.removeprefix('/data')}"
                self.assertEqual(config.get("Default"), host)
                self.assertEqual(config.text, host)
                self.assertEqual(config.get("Required"), expected_required)

    def test_in_repo_only_config_is_the_spoolman_url_choice(self):
        in_repo = self.configs_by_key(UNRAID)
        ca = self.configs_by_key(self.CA)
        self.assertEqual(set(in_repo) - set(ca),
                         {("Variable", "PRINT_HANDY__SERVICES__SPOOLMAN_URL")})

    def test_repository_and_listing_metadata_differ_as_documented(self):
        # The in-repo template is the release reference: it pins the
        # multi-arch index digest. The CA template is the listing: it
        # names the release tag DOCKER.md pins as current, offers the
        # nightly/develop branches, and carries the listing metadata
        # the in-repo file deliberately leaves empty.
        docker_doc = (ROOT / "docs" / "DOCKER.md").read_text()
        current_release = re.search(r"The current release is (\d+\.\d+\.\d+)\.", docker_doc)
        if current_release is None:
            self.fail("docs/DOCKER.md does not name the current release")
        self.assertRegex(UNRAID.findtext("Repository") or "",
                         r"^ghcr\.io/catduckgnaf/print-handy@sha256:[0-9a-f]{64}$")
        self.assertEqual(self.CA.findtext("Repository"),
                         f"ghcr.io/catduckgnaf/print-handy:{current_release.group(1)}")
        self.assertEqual([branch.findtext("Tag") for branch in self.CA.findall("Branch")],
                         [current_release.group(1), "nightly", "develop"])
        self.assertEqual(self.CA.findtext("Registry"),
                         "https://github.com/catduckgnaf/print-handy/pkgs/container/print-handy")
        self.assertEqual(self.CA.findtext("TemplateURL"),
                         "https://raw.githubusercontent.com/catduckgnaf/catduck-unraid-templates/main/templates/print-handy.xml")
        self.assertEqual(self.CA.findtext("Icon"),
                         "https://raw.githubusercontent.com/catduckgnaf/catduck-unraid-templates/main/templates/img/print-handy.png")
        for tag in ("Registry", "TemplateURL", "Icon"):
            self.assertFalse(UNRAID.findtext(tag))
        self.assertEqual(UNRAID.findtext("Support"),
                         "https://github.com/catduckgnaf/print-handy/issues")
        self.assertEqual(self.CA.findtext("Support"),
                         "https://forums.unraid.net/topic/138058-support-catduck-templates/")

    def test_requires_texts_carry_the_documented_guidance(self):
        # The CA Requires additionally carries the upgrade and recovery
        # warnings for installs created while the image declared
        # volumes, including the dot-config rename; both state the
        # ownership requirement.
        ca_requires = self.CA.findtext("Requires") or ""
        self.assertIn("anonymous volumes", ca_requires)
        self.assertIn("mv dot-config .config", ca_requires)
        for root in (UNRAID, self.CA):
            self.assertIn("1000", root.findtext("Requires") or "")


class UnraidHostPathDocsTests(unittest.TestCase):
    """#1143: the docs name the host folders an install really uses.

    The single-path Unraid template maps /data/.config to `.config` in the
    Appdata folder; only Compose (and the older multi-path template, whose
    upgrade note renames it) uses a host folder named `dot-config`. A doc
    sending an Unraid user to appdata `dot-config` points at a folder the
    container never reads. Counts of Compose paths in prose drift whenever
    a mount is added or dropped, so any count stated must match Compose."""

    NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

    def test_settings_doc_names_the_unraid_config_folder(self):
        self.assertNotRegex(DOCS, r"Unraid template maps to appdata `dot-config`")
        self.assertNotRegex(DOCS, r"appdata `dot-config")
        self.assertIn("`.config/print-handy/settings.yaml` in the Unraid Appdata folder", DOCS)

    def test_docker_doc_compose_path_count_matches_compose(self):
        docker_doc = (ROOT / "docs" / "DOCKER.md").read_text()
        sentences = re.findall(r"recreate with the same (?:(\w+) )?paths", docker_doc)
        self.assertTrue(sentences, "docs/DOCKER.md lost its Compose upgrade sentence")
        mounts = len(COMPOSE["services"]["combined"]["volumes"])
        for word in sentences:
            if word:
                with self.subTest(count=word):
                    self.assertEqual(self.NUMBERS.get(word.lower()), mounts)


class WebuiContainerTests(unittest.TestCase):
    """#286 stages 4-5: the Web UI is a gated supervised child, a
    healthcheck row, Compose variables and Unraid template fields —
    on by default in Compose, with its port published."""

    def variables(self):
        return {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}

    def test_compose_webui_variables_default_to_on_8088(self):
        # On inside the container on 8088, the port published by default,
        # and the first visit serves the Web UI and its walkthrough
        # openly — a password exists only if one is deliberately set.
        compose_env = COMPOSE["services"]["combined"]["environment"]
        self.assertEqual(compose_env["PRINT_HANDY__WEBUI__ENABLED"],
                         "${PRINT_HANDY__WEBUI__ENABLED:-true}")
        self.assertEqual(compose_env["PRINT_HANDY__WEBUI__HOST"],
                         "${PRINT_HANDY__WEBUI__HOST:-0.0.0.0}")
        self.assertEqual(compose_env["PRINT_HANDY__WEBUI__PORT"],
                         "${PRINT_HANDY__WEBUI__PORT:-8088}")
        # The token is a secret: .env env_file only, never a Compose default.
        self.assertNotIn("WEBUI_ACCESS_TOKEN", compose_env)
        # The port publication is active in the file text, not a comment.
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('      - "8088:${PRINT_HANDY__WEBUI__PORT:-8088}"', text)
        self.assertEqual(COMPOSE["services"]["combined"]["ports"],
                         ["8088:${PRINT_HANDY__WEBUI__PORT:-8088}"])

    def test_compose_does_not_hard_default_the_web_chat_toggles(self):
        """P2-88: no Compose environment entry for the web chat toggles.

        A ${...:-true} entry would be applied after the mounted config
        and silently undo a chat_enabled / chat_llm_enabled the operator
        set false in their config file (the compose file's own P2-88
        comment, on the visual-watch toggles, and the #1098
        starter-mention omission are the same rule). Unset, the shipped
        config defaults stand; set in .env, the env_file carries the
        value into the container — the variables stay settable, just
        never hard-defaulted here.
        """
        compose_env = COMPOSE["services"]["combined"]["environment"]
        self.assertNotIn("PRINT_HANDY__WEBUI__CHAT_ENABLED", compose_env)
        self.assertNotIn("PRINT_HANDY__WEBUI__CHAT_LLM_ENABLED", compose_env)
        # The reason is written where the next editor will look for the
        # missing entries: the Web UI block's own comment.
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("P2-88", text)
        self.assertIn("PRINT_HANDY__WEBUI__CHAT_ENABLED", text)

    def test_unraid_template_carries_no_webui_settings(self):
        """The slimmed template's only Web UI surface is the 8088 port
        mapping (pinned in UnraidTemplateTests). The enablement #440 put
        in template variables now lives in the shipped container config
        and Compose (test_compose_webui_variables_default_to_on_8088);
        the access token is a profile-.env secret, never a template
        field."""
        variables = self.variables()
        for target in ("PRINT_HANDY__WEBUI__ENABLED", "PRINT_HANDY__WEBUI__HOST",
                       "PRINT_HANDY__WEBUI__PORT", "WEBUI_ACCESS_TOKEN"):
            with self.subTest(variable=target):
                self.assertNotIn(target, variables)

    def test_supervisor_and_healthcheck_carry_the_gate_and_row(self):
        supervisor = (ROOT / "docker" / "supervise.sh").read_text()
        self.assertIn("python3 -m print_handy.webui_gate", supervisor)
        self.assertIn("python3 -m print_handy.webui &", supervisor)
        probe = (ROOT / "docker" / "healthcheck.py").read_text()
        self.assertIn("from print_handy.webui_gate import", probe)
        self.assertIn("webui_config", probe)
        self.assertIn("start_blocked", probe)
        self.assertIn("webui: ", probe)

    def test_webui_is_documented_in_the_docker_guide(self):
        docker_doc = (ROOT / "docs" / "DOCKER.md").read_text()
        self.assertIn("## Web UI (optional)", docker_doc)
        self.assertIn("PRINT_HANDY__WEBUI__ENABLED", docker_doc)
        self.assertIn("WEBUI_ACCESS_TOKEN", docker_doc)


class SpoolmanLanTests(unittest.TestCase):
    """#443: bundled Spoolman LAN access is opt-in. The safe default is
    pinned everywhere: SPOOLMAN_HOST 127.0.0.1, no active Compose port
    mapping — so with no operator action Spoolman stays loopback-only
    and unexposed. On Unraid the mapping and the variable are added by
    hand since the template slimming; Compose keeps them as comments."""

    def configs(self, kind):
        return {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == kind}

    def test_unraid_template_leaves_the_opt_in_to_the_operator(self):
        """The slimmed template ships no Spoolman port mapping and no
        SPOOLMAN_HOST variable: an Unraid operator who wants LAN access
        adds both by hand in the container editor (docs/DOCKER.md,
        Included Spoolman). The template's one Spoolman entry is the
        bundled/external URL choice, in Advanced View."""
        ports = self.configs("Port")
        self.assertNotIn("8000", ports)
        variables = self.configs("Variable")
        self.assertNotIn("SPOOLMAN_HOST", variables)
        self.assertNotIn("PRINT_HANDY_BUNDLED_SPOOLMAN", variables)
        url = variables["PRINT_HANDY__SERVICES__SPOOLMAN_URL"]
        self.assertEqual(url.text, "http://127.0.0.1:8000")
        self.assertEqual(url.get("Display"), "advanced")

    def test_compose_publishes_only_the_web_ui_port_and_keeps_spoolman_on_loopback(self):
        service = COMPOSE["services"]["combined"]
        # The only active port mapping is the Web UI's; the Spoolman LAN
        # mapping stays a comment and Spoolman stays loopback by default.
        self.assertEqual(service["ports"], ["8088:${PRINT_HANDY__WEBUI__PORT:-8088}"])
        self.assertEqual(service["environment"]["SPOOLMAN_HOST"],
                         "${SPOOLMAN_HOST:-127.0.0.1}")
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('#   - "7912:8000"', text)
        self.assertIn("SPOOLMAN_HOST=0.0.0.0", text)
        self.assertRegex(ENV_EXAMPLE, r"(?m)^# SPOOLMAN_HOST=127\.0\.0\.1$")

    def test_supervisor_binds_spoolman_from_spoolman_host(self):
        supervisor = (ROOT / "docker" / "supervise.sh").read_text()
        self.assertIn("SPOOLMAN_HOST", supervisor)

    def test_lan_access_is_documented(self):
        docker_doc = (ROOT / "docs" / "DOCKER.md").read_text()
        self.assertIn("Optional LAN access:", docker_doc)
        self.assertIn("http://<unraid-ip>:7912", docker_doc)
        self.assertIn("/api/v1/info", docker_doc)
        zmod_doc = (ROOT / "docs" / "ZMOD.md").read_text()
        self.assertIn("Bundled Spoolman from printer Moonraker", zmod_doc)
        self.assertIn("server: http://<unraid-ip>:7912", zmod_doc)


class LiveContainerTests(unittest.TestCase):
    """#311 stage 4: the live-view Activity server is a gated supervised
    child, a healthcheck row, Compose opt-in variables and an optional
    profile-gated cloudflared sidecar — and stays off everywhere by
    default. The slimmed Unraid template carries no live fields; on
    Unraid they are config-file and .env settings like everything else."""

    def variables(self):
        return {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}

    def test_compose_live_variables_default_to_off(self):
        compose_env = COMPOSE["services"]["combined"]["environment"]
        self.assertEqual(compose_env["PRINT_HANDY__LIVE_STREAM__ENABLED"],
                         "${PRINT_HANDY__LIVE_STREAM__ENABLED:-false}")
        self.assertEqual(compose_env["PRINT_HANDY__LIVE_STREAM__PORT"],
                         "${PRINT_HANDY__LIVE_STREAM__PORT:-0}")
        self.assertEqual(compose_env["DISCORD_CLIENT_ID"], "${DISCORD_CLIENT_ID:-}")
        # The secrets reach the container through the .env env_file only,
        # never a Compose environment entry.
        for secret in ("DISCORD_CLIENT_SECRET", "LIVE_STREAM_SESSION_SECRET"):
            self.assertNotIn(secret, compose_env)

    def test_cloudflared_sidecar_is_profile_gated_and_shares_loopback(self):
        sidecar = COMPOSE["services"]["cloudflared"]
        self.assertEqual(sidecar["profiles"], ["live-stream"])
        self.assertEqual(sidecar["network_mode"], "service:combined")
        # Render-safe ${VAR:-default} forms, never :? guards: a required
        # variable on a profile-gated service breaks the bare
        # `docker compose config` render (the docker-static CI job). The
        # token has no safe default; the profile is the opt-in and
        # cloudflared itself exits when started without one.
        self.assertEqual(sidecar["environment"]["TUNNEL_TOKEN"], "${TUNNEL_TOKEN:-}")
        self.assertEqual(sidecar["image"], "${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:2026.10.7@sha256:4955524aeaa379e154023d5a027c250f7b35041a2e7e950cf13f144bc9b9c5ba}")
        self.assertNotIn(":latest", sidecar["image"])
        self.assertNotIn("ports", sidecar)

    def test_unraid_template_carries_no_live_fields(self):
        variables = self.variables()
        for target in ("PRINT_HANDY__LIVE_STREAM__ENABLED", "PRINT_HANDY__LIVE_STREAM__PORT",
                       "PRINT_HANDY__LIVE_STREAM__VOICE_CHANNEL_ID",
                       "PRINT_HANDY__LIVE_STREAM__ACTIVITY_ORIGIN",
                       "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET",
                       "LIVE_STREAM_SESSION_SECRET", "TUNNEL_TOKEN"):
            with self.subTest(variable=target):
                self.assertNotIn(target, variables)

    def test_supervisor_and_healthcheck_carry_the_gate_and_row(self):
        supervisor = (ROOT / "docker" / "supervise.sh").read_text()
        self.assertIn("python3 -m print_handy.live_gate", supervisor)
        self.assertIn("python3 -m print_handy.live_activity &", supervisor)
        probe = (ROOT / "docker" / "healthcheck.py").read_text()
        self.assertIn("from print_handy.live_gate import live_config", probe)
        self.assertIn("live view: ", probe)

    def test_live_view_is_documented_in_the_docker_guide(self):
        docker_doc = (ROOT / "docs" / "DOCKER.md").read_text()
        self.assertIn("## Live camera in Discord (optional)", docker_doc)
        self.assertIn("PRINT_HANDY__LIVE_STREAM__ENABLED", docker_doc)
        self.assertIn("TUNNEL_TOKEN", docker_doc)


class CameraWatchVisibilityTests(unittest.TestCase):
    """#248: the camera watchdog is advertised, and visual_watch sensitivity is explicit
    and configurable in the shipped YAML/TOML Docker configs and Compose. The slimmed
    Unraid template carries no visual_watch variables, so a mounted config's watchdog
    settings stand there exactly as written."""

    def test_docker_configs_carry_visual_watch_with_the_documented_defaults(self):
        docker_yaml = yaml.safe_load((ROOT / "config" / "docker.yaml").read_text())
        self.assertEqual(docker_yaml["visual_watch"], {"sensitivity": 3, "enabled": True,
                                                        "early_layers": 10, "early_interval_s": 120,
                                                        "auto_pause_early": False})
        docker_toml = tomllib.loads((ROOT / "config" / "docker.toml").read_text())
        self.assertEqual(docker_toml["visual_watch"], {"sensitivity": 3, "enabled": True,
                                                        "early_layers": 10, "early_interval_s": 120,
                                                        "auto_pause_early": False})
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={})
        self.assertEqual(loaded["visual_watch"]["sensitivity"], 3)
        self.assertIs(loaded["visual_watch"]["enabled"], True)
        overridden = settings.load(local=ROOT / "config" / "docker.yaml", environ={
            "PRINT_HANDY__VISUAL_WATCH__SENSITIVITY": "5",
            "PRINT_HANDY__VISUAL_WATCH__ENABLED": "false"})
        self.assertEqual(overridden["visual_watch"]["sensitivity"], 5)
        self.assertIs(overridden["visual_watch"]["enabled"], False)

    def test_compose_defaults_agree_and_unraid_carries_none(self):
        compose_env = COMPOSE["services"]["combined"]["environment"]
        # P2-88: Compose must not inject watchdog defaults of its own. A
        # ${...:-true} / ${...:-3} entry is applied by the loader after the
        # mounted config, so it silently undid a watchdog the operator had
        # disabled there; a blank ${...:-} entry is no fix either — the
        # loader deliberately rejects blank booleans/numbers. Leaving the
        # variables out of environment: entirely keeps unspecified
        # overrides absent: the mounted config (or the shipped defaults
        # checked above) stands, and an operator who sets them in .env
        # still overrides, via the env_file both services already declare.
        self.assertNotIn("PRINT_HANDY__VISUAL_WATCH__SENSITIVITY", compose_env)
        self.assertNotIn("PRINT_HANDY__VISUAL_WATCH__ENABLED", compose_env)
        env_example = (ROOT / ".env.example").read_text()
        self.assertIn("# PRINT_HANDY__VISUAL_WATCH__SENSITIVITY=3", env_example)
        self.assertIn("# PRINT_HANDY__VISUAL_WATCH__ENABLED=true", env_example)
        variables = {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}
        for target in ("PRINT_HANDY__VISUAL_WATCH__SENSITIVITY",
                       "PRINT_HANDY__VISUAL_WATCH__ENABLED"):
            with self.subTest(variable=target):
                # The slimmed template injects nothing: Unraid passes a
                # variable's value explicitly only when the template
                # carries it, so the shipped-config defaults above are
                # the whole story there — the same end state the P2-88
                # Compose fix produced.
                self.assertNotIn(target, variables)

    def test_mounted_watchdog_settings_stand_without_env_overrides(self):
        """P2-88 end to end: with the watchdog variables absent from the
        container environment (the state the Compose fix produces), a
        mounted config's watchdog settings load exactly as written."""
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "mounted.yaml"
            local.write_text("visual_watch:\n  enabled: false\n  sensitivity: 1\n")
            loaded = settings.load(local=local, environ={})
        self.assertIs(loaded["visual_watch"]["enabled"], False)
        self.assertEqual(loaded["visual_watch"]["sensitivity"], 1)

    def test_compose_does_not_hard_default_the_starter_mention(self):
        """#1095 review: Compose must not inject the starter-mention default.

        The ${...:-true} entry for the renamed setting was applied after
        the .env env_file, so an existing install whose .env carries the
        pre-#1095 spelling set to false got the new variable forced to
        true in the container and the loader's alias never saw the
        opt-out — the same failure shape as P2-88 above. Leaving the
        setting out of environment: entirely keeps unspecified overrides
        absent (the shipped default, true, stands), and an operator who
        sets either spelling in .env still overrides via the env_file.
        """
        compose_env = COMPOSE["services"]["combined"]["environment"]
        self.assertNotIn("PRINT_HANDY__DISCORD__MENTION_STARTER", compose_env)
        self.assertNotIn("PRINT_HANDY__DISCORD__MENTION_STARTER_ON_COMPLETE", compose_env)
        # The env_file state an existing install produces: only the old
        # spelling, set false. The alias folds it onto the renamed key.
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={
            "PRINT_HANDY__DISCORD__MENTION_STARTER_ON_COMPLETE": "false"})
        self.assertIs(loaded["discord"]["mention_starter"], False)
        # Neither spelling set: the documented default stands.
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={})
        self.assertIs(loaded["discord"]["mention_starter"], True)
        # Both spellings set: the current name wins, in either direction.
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={
            "PRINT_HANDY__DISCORD__MENTION_STARTER_ON_COMPLETE": "false",
            "PRINT_HANDY__DISCORD__MENTION_STARTER": "true"})
        self.assertIs(loaded["discord"]["mention_starter"], True)
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={
            "PRINT_HANDY__DISCORD__MENTION_STARTER_ON_COMPLETE": "true",
            "PRINT_HANDY__DISCORD__MENTION_STARTER": "false"})
        self.assertIs(loaded["discord"]["mention_starter"], False)
        # The in-repo Unraid template no longer injects the variable at
        # all: #1108 slimmed it to the CA set plus Spoolman, so neither
        # spelling is forced there either — an install's value reaches
        # the loader through its .env / container env, as above.
        variables = {c.get("Target"): c for c in UNRAID.findall("Config")
                     if c.get("Type") == "Variable"}
        self.assertNotIn("PRINT_HANDY__DISCORD__MENTION_STARTER", variables)
        self.assertNotIn("PRINT_HANDY__DISCORD__MENTION_STARTER_ON_COMPLETE", variables)

    def test_compose_does_not_hard_default_the_webui_access_settings(self):
        """P2-88: no Compose environment entry for the Web UI access settings.

        require_auth, controls_enabled and cookie_secure are fields on the
        Web UI Settings page. A ${...:-default} entry would be applied after
        the mounted config, override what the page saved there and lock the
        field. Unset, the mounted config's values stand; set in .env, the
        env_file still carries the value into the container.
        """
        compose_env = COMPOSE["services"]["combined"]["environment"]
        for name in ("PRINT_HANDY__WEBUI__REQUIRE_AUTH",
                     "PRINT_HANDY__WEBUI__CONTROLS_ENABLED",
                     "PRINT_HANDY__WEBUI__COOKIE_SECURE"):
            self.assertNotIn(name, compose_env)
        # The env_file path still works: the loader honors each variable.
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={
            "PRINT_HANDY__WEBUI__REQUIRE_AUTH": "false",
            "PRINT_HANDY__WEBUI__CONTROLS_ENABLED": "true",
            "PRINT_HANDY__WEBUI__COOKIE_SECURE": "true"})
        self.assertIs(loaded["webui"]["require_auth"], False)
        self.assertIs(loaded["webui"]["controls_enabled"], True)
        self.assertIs(loaded["webui"]["cookie_secure"], True)
        # Nothing set: the mounted config's values stand.
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={})
        self.assertIs(loaded["webui"]["require_auth"], True)
        self.assertIs(loaded["webui"]["controls_enabled"], False)
        self.assertIs(loaded["webui"]["cookie_secure"], False)

    def test_watchdog_is_advertised_where_users_look(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("Camera watchdog", readme)
        self.assertIn("spaghetti", readme.lower())
        self.assertIn("PRINT_HANDY__VISUAL_WATCH__SENSITIVITY", readme)
        docker_doc = (ROOT / "docs" / "DOCKER.md").read_text()
        self.assertIn("## Camera watch (visual_watch)", docker_doc)
        self.assertIn("PRINT_HANDY__VISUAL_WATCH__ENABLED", docker_doc)
        self.assertIn("PRINT_HANDY__VISUAL_WATCH__SENSITIVITY", docker_doc)


FEATURE_SETTINGS = {
    "PRINT_HANDY__SERVICES__VISION_CHAT_URL": "",
    "PRINT_HANDY__SERVICES__CAMERA_SNAPSHOT_URL": "",
    "PRINT_HANDY__VISION__MODEL": "gemini-latest",
    "PRINT_HANDY__VISION__ROAST_ENABLED": "true",
    "PRINT_HANDY__VISION__AUTO_LIGHT_OFF_MINUTES": "0",
    "PRINT_HANDY__VISION__LIGHT_OBJECT": "",
    "PRINT_HANDY__FLASHFORGE_STOCK__HOST": "",
    "PRINT_HANDY__BAMBU__LAN_HOST": "",
    "PRINT_HANDY__BAMBU__LAN_SERIAL": "",
    "PRINT_HANDY__DISCORD__APPROVAL_USER_ID": "",
    "PRINT_HANDY__DISCORD__TIMEZONE": "America/New_York",
    "PRINT_HANDY__LLM__CHAT_URL": "",
    "PRINT_HANDY__LLM__MODEL": "",
    # Live camera in Discord (#311): every [live_stream] setting except the
    # port — the settings/Compose default for the port stays 0 (no port
    # chosen), pinned separately in LiveContainerTests below.
    "PRINT_HANDY__LIVE_STREAM__ENABLED": "false",
    "PRINT_HANDY__LIVE_STREAM__VOICE_CHANNEL_ID": "",
    "PRINT_HANDY__LIVE_STREAM__AUTO_START_ON_PRINT": "false",
    "PRINT_HANDY__LIVE_STREAM__MAX_HEIGHT": "720",
    "PRINT_HANDY__LIVE_STREAM__FPS": "15",
    "PRINT_HANDY__LIVE_STREAM__MAX_BITRATE_KBPS": "2500",
    "PRINT_HANDY__LIVE_STREAM__ENCODER": "auto",
    "PRINT_HANDY__LIVE_STREAM__EMPTY_TIMEOUT_S": "300",
    "PRINT_HANDY__LIVE_STREAM__ACTIVITY_ORIGIN": "",
}
FEATURE_SECRETS = {"CLIPROXY_API_KEY", "AD5X_SERIAL", "AD5X_CHECK_CODE",
                   "BAMBU_LAN_PASSWORD", "MOONRAKER_API_KEY", "LLM_API_KEY",
                   "DISCORD_CLIENT_SECRET", "LIVE_STREAM_SESSION_SECRET", "TUNNEL_TOKEN",
           "CREALITYCLOUD_TOKEN", "THANGS_TOKEN", "NEXPRINT_TOKEN",
           "MYMINIFACTORY_OAUTH_TOKEN", "MAKERWORLD_TOKEN", "THINGIVERSE_TOKEN", "CULTS3D_TOKEN"}


class DockerFeatureVariablesTests(unittest.TestCase):
    """#252: the feature-enabling variables are in the Compose environment
    with agreeing defaults; their secrets reach the container through the
    .env file only. The Unraid template carried them until the
    install-surface slimming; it now carries none of them — on Unraid
    every one is set in the mounted config file, the Web UI, or the
    profile .env inside the Appdata path."""

    def variables(self):
        return {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}

    def test_settings_are_in_compose_with_agreeing_defaults(self):
        compose_env = COMPOSE["services"]["combined"]["environment"]
        for name, default in FEATURE_SETTINGS.items():
            with self.subTest(variable=name):
                self.assertEqual(compose_env[name], "${%s:-%s}" % (name, default))
                section, key = name[len("PRINT_HANDY__"):].lower().split("__")
                self.assertIn((section, key), inventory())

    def test_secrets_stay_out_of_compose_environment_and_the_template(self):
        compose_env = COMPOSE["services"]["combined"]["environment"]
        variables = self.variables()
        for name in sorted(FEATURE_SECRETS):
            with self.subTest(secret=name):
                self.assertIn(name, SECRETS)  # SETTINGS.md table and .env.example line, pinned by parity tests
                self.assertNotIn(name, variables)
                self.assertNotIn(name, compose_env)

    def test_template_carries_none_of_the_feature_settings(self):
        variables = self.variables()
        for name in FEATURE_SETTINGS:
            with self.subTest(variable=name):
                self.assertNotIn(name, variables)
        # Printer model selection likewise left the template: it is a
        # config-file / Web UI catalog choice now, defaulting to
        # klipper.generic in the shipped Docker config.
        self.assertNotIn("PRINT_HANDY__PRINTER__MODEL", variables)


class AcceptanceFixtureTests(unittest.TestCase):
    def test_overlay_isolates_services_and_gives_the_fake_printer_a_readiness_check(self):
        overlay = yaml.safe_load((ROOT / "tests" / "docker" / "compose.acceptance.yml").read_text())
        self.assertTrue(overlay["networks"]["isolated"]["internal"])
        self.assertEqual(set(overlay["services"]), {"combined", "fake-moonraker"})
        for name in ("combined", "fake-moonraker"):
            with self.subTest(service=name):
                service = overlay["services"][name]
                self.assertEqual(service["networks"], ["isolated"])
                self.assertEqual(service["restart"], "no")
                self.assertNotIn("ports", service)
        fake = overlay["services"]["fake-moonraker"]
        self.assertIn("/server/info", json.dumps(fake["healthcheck"]["test"]))
        self.assertEqual(overlay["services"]["combined"]["depends_on"]["fake-moonraker"]["condition"], "service_healthy")
        self.assertFalse((ROOT / "tests" / "docker" / "compose.acceptance-spoolman.yml").exists())

    def test_harness_owns_its_names_and_publishes_no_port(self):
        script = (ROOT / "tests" / "docker" / "image_acceptance.sh").read_text()
        self.assertIn('project="ph-accept-${mode}-${run_id}"', script)
        self.assertNotIn("container_name", script)
        self.assertNotRegex(script, r"\s-p\s+\d|--publish")
        self.assertNotIn("prune", script)
        self.assertIn("not a version upgrade", script)
        self.assertNotIn("print-handy-bot.xml", script)
        for check in ("spoolman:loopback-only", "spoolman:no-web-ui", "spoolman:persisted", "bot:fails-closed"):
            with self.subTest(check=check):
                self.assertIn(check, script)

    def test_lifecycle_gate_is_scratch_only_and_covers_every_transition(self):
        """The release lifecycle gate (install, upgrade, restore, downgrade) never touches real data."""
        script = (ROOT / "tests" / "docker" / "lifecycle_gate.sh").read_text()
        self.assertIn('-p "ph-life-$name-$run_id"', script)
        self.assertIn("work=$(mktemp -d)", script)
        self.assertNotIn("container_name", script)
        self.assertNotRegex(script, r"\s-p\s+\d|--publish")
        for forbidden in ("prune", "docker push", "docker pull", "docker build", "/mnt/user", "appdata"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)
        # Helper containers have no network; Compose phases use the internal acceptance network.
        self.assertIn("--network none", script)
        self.assertIn("compose.acceptance.yml", script)
        helper = (ROOT / "tests" / "docker" / "lifecycle_backup.py").read_text()
        self.assertIn("lifecycle_backup.py backup /tree /backup", script)
        self.assertIn("lifecycle_backup.py restore /backup /tree", script)
        self.assertIn("PRAGMA integrity_check", helper)
        self.assertIn("SHA256SUMS", helper)
        # Every transition runs, each with its own health and data checks.
        for literal in ("start fresh", "fresh:schema", "upgrade:seeded", "backup:verified",
                        "start upgrade", "for step in restore downgrade", "image=$previous",
                        '"$1:healthy"', '"$1:data-intact"', '"$step:restored"'):
            with self.subTest(literal=literal):
                self.assertIn(literal, script)


class LifecycleGateCleanupTests(unittest.TestCase):
    """The gate's EXIT cleanup must collect logs into OUT_DIR and remove every Compose project.

    Runs the real script against a stub docker that behaves like Compose on the one point that
    matters: without PRINT_HANDY_IMAGE in the environment, every compose call fails. Every phase
    reports unhealthy, so the script exits early and only cleanup is exercised.
    """

    STUB = """#!/bin/sh
echo "$PWD|${PRINT_HANDY_IMAGE:-}|${PRINT_HANDY_DATA:-}|$*" >> "$STUB_LOG"
case "$1 $2" in
  "image inspect") echo sha256:0000000000000000000000; exit 0 ;;
  "compose "*) [ -n "${PRINT_HANDY_IMAGE:-}" ] || { echo "required variable PRINT_HANDY_IMAGE is missing" >&2; exit 1; }
     case " $* " in *" up "*) exit 1 ;; *" logs "*) echo stub-logs ;; esac ;;
esac
exit 0
"""

    def test_cleanup_logs_into_relative_out_dir_and_downs_every_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            for name, body in (("docker", self.STUB), ("sudo", "#!/bin/sh\nexit 0\n")):
                (bin_dir / name).write_text(body)
                (bin_dir / name).chmod(0o755)
            log = tmp / "docker.log"
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STUB_LOG=str(log), TMPDIR=str(tmp))
            env.pop("PRINT_HANDY_IMAGE", None)
            result = subprocess.run(["bash", str(ROOT / "tests" / "docker" / "lifecycle_gate.sh"),
                                     "prev:tag", "cand:tag", "evidence/lifecycle"],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn("compose down failed", result.stderr)
            calls = log.read_text().splitlines()
            downs = [c for c in calls if " down " in c]
            self.assertEqual(len(downs), 2, calls)  # fresh and prev were started
            for call in downs + [c for c in calls if " logs " in c]:
                _cwd, image, data, _args = call.split("|", 3)
                self.assertIn(image, ("prev:tag", "cand:tag"), call)
                self.assertTrue(data, call)
            out = tmp / "evidence" / "lifecycle"
            logs = sorted(out.glob("logs-ph-life-*.txt"))
            self.assertEqual([f.name.split("-")[3] for f in logs], ["fresh", "prev"], logs)
            for f in logs:
                self.assertEqual(f.read_text().strip(), "stub-logs")


class LifecycleBackupTests(unittest.TestCase):
    """The gate's backup must produce a manifest its own restore can match, WAL databases included."""

    def test_wal_database_backup_restores_against_its_manifest(self):
        sys.path.insert(0, str(ROOT / "tests" / "docker"))
        try:
            import lifecycle_backup
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "tree"
            (src / "spoolman").mkdir(parents=True)
            (src / "hermes").mkdir()
            (src / "hermes" / "marker.txt").write_text("seed")
            for db in (src / "spoolman" / "spoolman.db", src / "hermes" / "launch.db"):
                con = sqlite3.connect(db)
                con.execute("PRAGMA journal_mode=WAL")
                con.execute("CREATE TABLE t (v TEXT)")
                con.execute("INSERT INTO t VALUES ('kept')")
                con.commit()
                con.close()
            backup_dir = tmp / "backup"
            backup_dir.mkdir()
            dbs, files = lifecycle_backup.backup(str(src), str(backup_dir))
            self.assertEqual((dbs, files), (2, 3))
            manifest = (backup_dir / "SHA256SUMS").read_text()
            self.assertNotRegex(manifest, r"-(wal|shm)$|-(wal|shm)\n")
            self.assertEqual(sorted(p.name for p in (backup_dir / "tree").rglob("*") if p.is_file()),
                             ["launch.db", "marker.txt", "spoolman.db"])
            dst = tmp / "restored"
            dst.mkdir()
            self.assertEqual(lifecycle_backup.restore(str(backup_dir), str(dst)), 3)
            con = sqlite3.connect(dst / "spoolman" / "spoolman.db")
            self.assertEqual(con.execute("SELECT v FROM t").fetchall(), [("kept",)])
            con.close()


if __name__ == "__main__":
    unittest.main()
