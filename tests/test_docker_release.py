"""Issue #117 release residue on top of main's Docker contract (test_docker_release_contract.py).

Settings parity: every user-adjustable setting in the schema has a docs/SETTINGS.md row and, when it
is a scalar, an .env.example line; every PRINT_HANDY__ name in a deployment file is a real setting.
Sanitization: public deployment files carry no LAN addresses, machine paths, IDs or credentials.
Build: OrcaSlicer is prebuilt-only (both channels pinned by version, revision and SHA256);
bounded, validated parallelism remains only for the Bambu Studio source build. Unraid: the
single template matches the Compose runtime contract.
"""
import ipaddress
import json
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

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
           "OPENVIKING_API_KEY", "BAMBU_LAN_PASSWORD", "AD5X_SERIAL", "AD5X_CHECK_CODE", "WEBUI_ACCESS_TOKEN",
           "MQTT_PASSWORD", "NTFY_ACCESS_TOKEN",
           "DISCORD_CLIENT_SECRET", "LIVE_STREAM_SESSION_SECRET", "TUNNEL_TOKEN",
           "CREALITYCLOUD_TOKEN", "THANGS_TOKEN", "NEXPRINT_TOKEN",
           "MYMINIFACTORY_OAUTH_TOKEN", "MAKERWORLD_TOKEN", "THINGIVERSE_TOKEN", "CULTS3D_TOKEN"}
COMPOSE_VARS = {"PRINT_HANDY_IMAGE", "PRINT_HANDY_DATA", "PRINT_HANDY_SPOOLMAN_DATA", "PRINT_HANDY_CONFIG", "TZ",
                "DISCORD_CLIENT_ID", "CLOUDFLARED_IMAGE"}
RUNTIME_ENV_VARS = {"PRINT_HANDY_ORCA_CHANNEL", "PRINT_HANDY_BUNDLED_SPOOLMAN", "SPOOLMAN_HOST", "PRINT_VISION_URL", "PRINT_FUZZY_URL"}
DATA_TARGETS = {"/data/hermes", "/data/archive", "/data/records", "/data/cache"}


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


class SanitizedExamplesTests(unittest.TestCase):
    def test_no_lan_addresses_machine_paths_ids_or_credentials(self):
        for path in DEPLOYMENT_FILES:
            text = (ROOT / path).read_text()
            with self.subTest(file=path):
                for raw in re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", text):
                    address = ipaddress.ip_address(raw)
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
        build = DOCKERFILE.split("FROM debian:13-slim AS runtime")[0]
        self.assertIn('jobs="$(orca-build-jobs "$ORCA_BUILD_JOBS")"', build)      # Bambu Studio source build only
        self.assertIn('export CMAKE_BUILD_PARALLEL_LEVEL="$jobs"', build)       # every dependency's own build
        self.assertIn("cmake --build deps/build --parallel 1", build)             # one dependency at a time
        self.assertIn('cmake --build build --parallel "$jobs"', build)            # the application
        self.assertNotIn("$(nproc)", build)


class OrcaPrebuiltTests(unittest.TestCase):
    """OrcaSlicer is never built from source: both channels are pinned official prebuilts."""

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
        self.assertIn('sh /usr/local/bin/orca-prebuilt preview "$TARGETARCH"', DOCKERFILE)

    def test_both_channels_are_pinned_prebuilts(self):
        helper = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        # Both channels pin OrcaSlicer v2.5.0-beta: it is the shipped slicer
        # (the successor of the v2.5.0-alpha pinned in #356), and the preview
        # channel converges on it until upstream publishes a newer prerelease.
        for pin in ("version=2.5.0-beta", "revision=345efec64c7b95a05ce5cf96df0dde66a158835c",
                    "sha256_amd64=15b0e9f9d577bc7a84cfef4f1ecc6e16ef75bc253e500d772fcaa66f4c25b97e",
                    "asset_amd64=OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0-beta.AppImage",
                    "sha256_arm64=941ea21d012483770a582589590b539f34cbe1c041aa367f9008fbe8c3e6bcd9",
                    "asset_arm64=OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0-beta.AppImage"):
            with self.subTest(pin=pin):
                self.assertIn(pin, helper)
        self.assertIn("set -eu", helper)
        self.assertIn("sha256sum --check --strict -", helper)
        self.assertLess(helper.index("sha256sum --check"), helper.index("--appimage-extract"))
        self.assertIn("--appimage-extract", helper)
        self.assertNotIn("--appimage-extract-and-run", helper)
        self.assertNotIn("cmake", helper)

    def test_per_arch_pins_parse_to_the_official_assets_and_digests(self):
        """The script's pin table, parsed not eyeballed: channel x arch -> asset + SHA256.

        Revisions are per channel (shared by both architectures); the aarch64
        asset digests are the upstream GitHub release asset digests for the
        pinned versions, matching the amd64 pins' provenance.
        """
        helper = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        blocks = dict(re.findall(r"(stable|preview)\)\n(.*?)\n        ;;", helper, re.S))
        self.assertEqual(set(blocks), {"stable", "preview"})
        expected = {
            "stable": ("2.5.0-beta", "345efec64c7b95a05ce5cf96df0dde66a158835c", {
                "amd64": ("OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0-beta.AppImage",
                          "15b0e9f9d577bc7a84cfef4f1ecc6e16ef75bc253e500d772fcaa66f4c25b97e"),
                "arm64": ("OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0-beta.AppImage",
                          "941ea21d012483770a582589590b539f34cbe1c041aa367f9008fbe8c3e6bcd9"),
            }),
            "preview": ("2.5.0-beta", "345efec64c7b95a05ce5cf96df0dde66a158835c", {
                "amd64": ("OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0-beta.AppImage",
                          "15b0e9f9d577bc7a84cfef4f1ecc6e16ef75bc253e500d772fcaa66f4c25b97e"),
                "arm64": ("OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0-beta.AppImage",
                          "941ea21d012483770a582589590b539f34cbe1c041aa367f9008fbe8c3e6bcd9"),
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
        # "alpha" is refused here too: it is only an alias for the runtime
        # entrypoint (#600), never a build channel.
        for arch in ("amd64", "arm64"):
            for bad_channel in ("beta", "alpha"):
                with self.subTest(arch=arch, channel=bad_channel):
                    result = subprocess.run(["sh", str(script), bad_channel, arch],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("usage: orca-prebuilt.sh stable|preview amd64|arm64", result.stderr)

    def test_each_channel_has_its_own_wrapper_and_identity(self):
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
        """Debian 13 slim for every stage (Orca, go2rtc, Bambu, Spoolman, runtime); no stock UID 1000 user, so handy takes 1000."""
        self.assertEqual(re.findall(r"^FROM (debian:13-slim)(?: AS [\w-]+)?$", DOCKERFILE, re.M),
                         ["debian:13-slim"] * 5)
        runtime = DOCKERFILE.split("FROM debian:13-slim AS runtime", 1)[1]
        self.assertIn("useradd --uid 1000 --create-home handy", runtime)
        self.assertNotIn("ubuntu", DOCKERFILE)


class ContainerTimezoneTests(unittest.TestCase):
    """TZ: the image carries a default container timezone (America/New_York)
    that Compose, .env.example, the Unraid template and the docs all expose,
    and the entrypoint keeps a valid override while falling back on an
    unknown name."""

    def test_image_installs_the_tz_database_and_defaults_tz(self):
        runtime = DOCKERFILE.split("FROM debian:13-slim AS runtime", 1)[1]
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


class UnraidTemplateTests(unittest.TestCase):
    def configs(self, root, kind):
        return {c.get("Target"): c for c in root.findall("Config") if c.get("Type") == kind}

    def setUp(self):
        self.service = COMPOSE["services"]["combined"]
        params = shlex.split(UNRAID.findtext("ExtraParams"))
        self.params = params
        self.flags = {flag: params[i + 1] for i, flag in enumerate(params[:-1]) if flag.startswith("--")}

    def test_template_runs_the_combined_compose_service(self):
        # The combined service is the template's container; the only other
        # Compose service is the optional cloudflared sidecar (#311), which
        # is profile-gated, shares the combined network namespace and is a
        # separate Unraid container there — never part of this template.
        self.assertEqual(list(COMPOSE["services"]), ["combined", "cloudflared"])
        sidecar = COMPOSE["services"]["cloudflared"]
        self.assertEqual(sidecar["profiles"], ["live-stream"])
        self.assertEqual(sidecar["network_mode"], "service:combined")
        self.assertEqual(UNRAID.findtext("Name"), "print-handy")
        self.assertFalse(UNRAID.findtext("PostArgs"))
        self.assertNotIn("command", self.service)
        health = self.flags["--health-cmd"]
        self.assertEqual(["CMD", *shlex.split(health)], self.service["healthcheck"]["test"])
        publish = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()
        image = re.search(r"(?m)^      IMAGE: (ghcr\.io/[^\s]+)$", publish)
        self.assertIsNotNone(image)
        self.assertIn("docker buildx imagetools create -t \"$IMAGE:latest\"", publish)
        self.assertEqual(UNRAID.findtext("Repository"), f"{image.group(1)}:latest")
        self.assertEqual(UNRAID.findtext("Privileged"), "false")
        # The bundled Spoolman API stays on loopback by default: Compose
        # publishes no port at all (both mappings below are commented
        # examples only), and the template's Port configs are the Web UI's
        # (#286 stage 5) plus the advanced, opt-in Spoolman LAN mapping
        # (#443) — which answers nothing while SPOOLMAN_HOST keeps its
        # 127.0.0.1 default (see SpoolmanLanTests).
        self.assertNotIn("ports", self.service)
        self.assertNotIn("-p", self.params)
        self.assertFalse(any(p.startswith("--publish") for p in self.params))
        ports = self.configs(UNRAID, "Port")
        self.assertEqual(set(ports), {"8088", "8000"})
        self.assertEqual(ports["8088"].text, "8088")
        self.assertEqual(ports["8000"].text, "7912")
        self.assertEqual(UNRAID.findtext("WebUI"), "http://[IP]:[PORT:8088]/")

    def test_hardening_and_limits_match_compose(self):
        value, service = self.flags, self.service
        logs = [self.params[i + 1] for i, flag in enumerate(self.params[:-1]) if flag == "--log-opt"]
        self.assertEqual(value["--user"], service["user"])
        self.assertEqual(value["--cap-drop"], "ALL")
        self.assertIn(value["--security-opt"], service["security_opt"])
        # No resource limits ship: a pids/memory/cpus limit starved OrcaSlicer on a large host.
        for flag in ("--pids-limit", "--memory", "--cpus"):
            self.assertNotIn(flag, value)
        for key in ("pids_limit", "mem_limit", "cpus", "deploy"):
            self.assertNotIn(key, service)
        self.assertEqual(value["--shm-size"], service["shm_size"])
        self.assertEqual(value["--restart"], service["restart"])
        self.assertEqual(sorted(logs), sorted(f"{k}={v}" for k, v in service["logging"]["options"].items()))
        for flag, key in (("--health-interval", "interval"), ("--health-timeout", "timeout"),
                          ("--health-start-period", "start_period"), ("--health-retries", "retries")):
            with self.subTest(flag=flag):
                self.assertEqual(str(value[flag]), str(service["healthcheck"][key]))

    def test_paths_and_variables_match_the_runtime(self):
        compose_env = self.service["environment"]
        paths = self.configs(UNRAID, "Path")
        # /data/.config (account tokens) is an optional Unraid mapping; Compose uses an override file.
        self.assertEqual(set(paths) - {"/data/.config"}, DATA_TARGETS | {"/data/spoolman"})
        self.assertEqual(set(paths) - {"/data/.config"}, {v.split(":")[-1] for v in self.service["volumes"]})
        self.assertEqual(paths["/data/.config"].get("Required"), "false")
        self.assertTrue(all(p.text.startswith("/mnt/user/appdata/print-handy/") for p in paths.values()))
        variables = self.configs(UNRAID, "Variable")
        for key in ("PRINT_HANDY_CONFIG", "PRINT_HANDY__BOT__MODE", "PRINT_HANDY__SERVICES__SPOOLMAN_URL",
                    "PRINT_HANDY_BUNDLED_SPOOLMAN"):
            with self.subTest(variable=key):
                self.assertEqual(variables[key].text, str(compose_env[key]).split(":-")[-1].rstrip("}"))
        for target, config in variables.items():
            with self.subTest(variable=target):
                if target in SECRETS:
                    self.assertEqual(config.get("Mask"), "true")
                    self.assertFalse(config.text)
                elif target in RUNTIME_ENV_VARS:
                    self.assertEqual(config.text, str(compose_env[target]).split(":-")[-1].rstrip("}"))
                elif target.startswith("PRINT_HANDY__"):
                    section, key = target[len("PRINT_HANDY__"):].lower().split("__")
                    self.assertIn((section, key), inventory())
                else:
                    self.assertIn(target, COMPOSE_VARS)
        # The bot token may be empty: the listener and Spoolman run while the bot idles.
        self.assertEqual(variables["DISCORD_BOT_TOKEN"].get("Required"), "false")
        # The runtime Orca channel selector (#180): in the template, the Compose runtime
        # environment and as a settable .env.example line, so both reach preview without a rebuild.
        self.assertEqual(variables["PRINT_HANDY_ORCA_CHANNEL"].text, "stable")
        self.assertEqual(compose_env["PRINT_HANDY_ORCA_CHANNEL"], "${PRINT_HANDY_ORCA_CHANNEL:-stable}")
        self.assertRegex(ENV_EXAMPLE, r"(?m)^PRINT_HANDY_ORCA_CHANNEL=stable$")


class WebuiContainerTests(unittest.TestCase):
    """#286 stages 4-5: the optional Web UI is a gated supervised child, a
    healthcheck row, Compose opt-in variables and Unraid template fields —
    and stays off everywhere by default."""

    def variables(self):
        return {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}

    def test_compose_webui_variables_default_to_on_8088(self):
        # #708: on inside the container on 8088; reachable only once the port
        # is published, and then the first-run setup page asks for a password.
        compose_env = COMPOSE["services"]["combined"]["environment"]
        self.assertEqual(compose_env["PRINT_HANDY__WEBUI__ENABLED"],
                         "${PRINT_HANDY__WEBUI__ENABLED:-true}")
        self.assertEqual(compose_env["PRINT_HANDY__WEBUI__HOST"],
                         "${PRINT_HANDY__WEBUI__HOST:-0.0.0.0}")
        self.assertEqual(compose_env["PRINT_HANDY__WEBUI__PORT"],
                         "${PRINT_HANDY__WEBUI__PORT:-8088}")
        # The token is a secret: .env env_file only, never a Compose default.
        self.assertNotIn("WEBUI_ACCESS_TOKEN", compose_env)
        # The port publication stays a commented example in the file text.
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('#   - "8088:${PRINT_HANDY__WEBUI__PORT:-8088}"', text)

    def test_unraid_webui_variables_and_masked_token(self):
        variables = self.variables()
        # #440: the template enables the Web UI so its WebUI link works;
        # the gate still starts it only once the token below is set (the
        # bind host is not loopback). Compose/code defaults stay off.
        self.assertEqual(variables["PRINT_HANDY__WEBUI__ENABLED"].text, "true")
        # The template binds all interfaces so the port mapping and WebUI link
        # work once enabled; the token below is then mandatory (the gate
        # blocks the start without it and the server refuses that bind).
        self.assertEqual(variables["PRINT_HANDY__WEBUI__HOST"].text, "0.0.0.0")
        self.assertEqual(variables["PRINT_HANDY__WEBUI__PORT"].text, "8088")
        token = variables["WEBUI_ACCESS_TOKEN"]
        self.assertEqual(token.get("Mask"), "true")
        self.assertFalse(token.text)

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
        self.assertIn("## Web UI (optional, #286)", docker_doc)
        self.assertIn("PRINT_HANDY__WEBUI__ENABLED", docker_doc)
        self.assertIn("WEBUI_ACCESS_TOKEN", docker_doc)


class SpoolmanLanTests(unittest.TestCase):
    """#443: bundled Spoolman LAN access is opt-in on both deployment
    surfaces. The safe default is pinned everywhere: SPOOLMAN_HOST
    127.0.0.1, no active Compose port mapping — so with no operator
    action Spoolman stays loopback-only and unexposed."""

    def configs(self, kind):
        return {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == kind}

    def test_unraid_template_carries_the_opt_in(self):
        ports = self.configs("Port")
        spoolman = ports["8000"]
        self.assertEqual(spoolman.text, "7912")
        self.assertEqual(spoolman.get("Default"), "7912")
        self.assertEqual(spoolman.get("Display"), "advanced")
        variables = self.configs("Variable")
        host = variables["SPOOLMAN_HOST"]
        self.assertEqual(host.text, "127.0.0.1")
        self.assertEqual(host.get("Display"), "advanced")
        self.assertIn("0.0.0.0", host.get("Description"))

    def test_compose_keeps_the_safe_default(self):
        service = COMPOSE["services"]["combined"]
        # No active port mapping ships; the LAN mapping is a comment.
        self.assertNotIn("ports", service)
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
        self.assertIn("Optional LAN access (#443", docker_doc)
        self.assertIn("http://<unraid-ip>:7912", docker_doc)
        self.assertIn("/api/v1/info", docker_doc)
        zmod_doc = (ROOT / "docs" / "ZMOD.md").read_text()
        self.assertIn("Bundled Spoolman from printer Moonraker (#443)", zmod_doc)
        self.assertIn("server: http://<unraid-ip>:7912", zmod_doc)


class LiveContainerTests(unittest.TestCase):
    """#311 stage 4: the live-view Activity server is a gated supervised
    child, a healthcheck row, Compose opt-in variables, an optional
    profile-gated cloudflared sidecar and Unraid template fields — and
    stays off everywhere by default."""

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
        self.assertEqual(sidecar["image"], "${CLOUDFLARED_IMAGE:-cloudflare/cloudflared:latest}")
        self.assertNotIn("ports", sidecar)

    def test_unraid_live_variables_and_masked_secrets(self):
        variables = self.variables()
        self.assertEqual(variables["PRINT_HANDY__LIVE_STREAM__ENABLED"].text, "false")
        # The template pre-fills the fixed tunnel target port (the Web UI
        # port precedent); the settings default itself stays 0.
        self.assertEqual(variables["PRINT_HANDY__LIVE_STREAM__PORT"].text, "8091")
        for secret in ("DISCORD_CLIENT_SECRET", "LIVE_STREAM_SESSION_SECRET", "TUNNEL_TOKEN"):
            with self.subTest(secret=secret):
                self.assertEqual(variables[secret].get("Mask"), "true")
                self.assertFalse(variables[secret].text)

    def test_supervisor_and_healthcheck_carry_the_gate_and_row(self):
        supervisor = (ROOT / "docker" / "supervise.sh").read_text()
        self.assertIn("python3 -m print_handy.live_gate", supervisor)
        self.assertIn("python3 -m print_handy.live_activity &", supervisor)
        probe = (ROOT / "docker" / "healthcheck.py").read_text()
        self.assertIn("from print_handy.live_gate import live_config", probe)
        self.assertIn("live view: ", probe)

    def test_live_view_is_documented_in_the_docker_guide(self):
        docker_doc = (ROOT / "docs" / "DOCKER.md").read_text()
        self.assertIn("## Live camera in Discord (optional, #311)", docker_doc)
        self.assertIn("PRINT_HANDY__LIVE_STREAM__ENABLED", docker_doc)
        self.assertIn("TUNNEL_TOKEN", docker_doc)


class CameraWatchVisibilityTests(unittest.TestCase):
    """#248: the camera watchdog is advertised, and visual_watch sensitivity is explicit
    and configurable in the shipped YAML/TOML Docker configs, Compose and Unraid."""

    def test_docker_configs_carry_visual_watch_with_the_documented_defaults(self):
        docker_yaml = yaml.safe_load((ROOT / "config" / "docker.yaml").read_text())
        self.assertEqual(docker_yaml["visual_watch"], {"sensitivity": 3, "enabled": True})
        docker_toml = tomllib.loads((ROOT / "config" / "docker.toml").read_text())
        self.assertEqual(docker_toml["visual_watch"], {"sensitivity": 3, "enabled": True})
        loaded = settings.load(local=ROOT / "config" / "docker.yaml", environ={})
        self.assertEqual(loaded["visual_watch"]["sensitivity"], 3)
        self.assertIs(loaded["visual_watch"]["enabled"], True)
        overridden = settings.load(local=ROOT / "config" / "docker.yaml", environ={
            "PRINT_HANDY__VISUAL_WATCH__SENSITIVITY": "5",
            "PRINT_HANDY__VISUAL_WATCH__ENABLED": "false"})
        self.assertEqual(overridden["visual_watch"]["sensitivity"], 5)
        self.assertIs(overridden["visual_watch"]["enabled"], False)

    def test_compose_and_unraid_defaults_agree(self):
        compose_env = COMPOSE["services"]["combined"]["environment"]
        self.assertEqual(compose_env["PRINT_HANDY__VISUAL_WATCH__SENSITIVITY"],
                         "${PRINT_HANDY__VISUAL_WATCH__SENSITIVITY:-3}")
        self.assertEqual(compose_env["PRINT_HANDY__VISUAL_WATCH__ENABLED"],
                         "${PRINT_HANDY__VISUAL_WATCH__ENABLED:-true}")
        variables = {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}
        for target, default in (("PRINT_HANDY__VISUAL_WATCH__SENSITIVITY", "3"),
                                ("PRINT_HANDY__VISUAL_WATCH__ENABLED", "true")):
            with self.subTest(variable=target):
                self.assertEqual(variables[target].text, default)
                self.assertEqual(variables[target].text,
                                 str(compose_env[target]).split(":-")[-1].rstrip("}"))

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
    # port — the template pre-fills 8091 as the fixed tunnel target while
    # the settings/Compose default stays 0 (the Web UI port precedent),
    # pinned separately in LiveContainerTests below.
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
    """#252: the feature-enabling variables are in the Unraid template and the Compose
    environment with agreeing defaults; their secrets are masked template fields only."""

    def variables(self):
        return {c.get("Target"): c for c in UNRAID.findall("Config") if c.get("Type") == "Variable"}

    def test_settings_are_in_template_and_compose_with_agreeing_defaults(self):
        compose_env = COMPOSE["services"]["combined"]["environment"]
        variables = self.variables()
        for name, default in FEATURE_SETTINGS.items():
            with self.subTest(variable=name):
                self.assertEqual(compose_env[name], "${%s:-%s}" % (name, default))
                self.assertEqual(variables[name].text or "", default)
                section, key = name[len("PRINT_HANDY__"):].lower().split("__")
                self.assertIn((section, key), inventory())

    def test_secrets_are_masked_template_fields_and_stay_out_of_compose_environment(self):
        compose_env = COMPOSE["services"]["combined"]["environment"]
        variables = self.variables()
        for name in sorted(FEATURE_SECRETS):
            with self.subTest(secret=name):
                self.assertIn(name, SECRETS)  # SETTINGS.md table and .env.example line, pinned by parity tests
                self.assertEqual(variables[name].get("Mask"), "true")
                self.assertFalse(variables[name].text)
                self.assertNotIn(name, compose_env)

    def test_template_model_list_names_every_supported_model(self):
        model = self.variables()["PRINT_HANDY__PRINTER__MODEL"]
        for name in ("flashforge.ad5x", "sovol.sv06", "klipper.generic",
                     "octoprint.marlin", "bambulab.a1", "bambulab.p1s"):
            with self.subTest(model=name):
                self.assertIn(name, model.get("Default"))
                self.assertIn(name, model.get("Description"))


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


if __name__ == "__main__":
    unittest.main()
