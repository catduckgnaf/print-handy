"""Issue #117 packaging regressions, runnable without a container engine."""
from pathlib import Path
import os
import shlex
import re
import subprocess
import tempfile
import unittest

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class DockerReleaseContract(unittest.TestCase):
    def test_standalone_is_default_runtime(self):
        """One supervised container: tini, then the supervisor that starts every required process."""
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("PRINT_HANDY__BOT__MODE=standalone", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/app/docker/entrypoint.sh", '
                      '"/usr/local/bin/print-handy-supervise"]', dockerfile)
        self.assertIn('CMD ["python3", "/usr/local/bin/print-handy-healthcheck"]', dockerfile)
        # The probe is installed outside /app; a script invocation puts the script's
        # own directory on sys.path, so it must add /app itself before importing
        # print_handy (regression: the combined container could never report healthy).
        probe = (ROOT / "docker" / "healthcheck.py").read_text()
        self.assertLess(probe.index('sys.path.insert(0, "/app")'),
                        probe.index("from print_handy.bot.health import"))
        supervisor = (ROOT / "docker" / "supervise.sh").read_text()
        # #299: the supervisor starts the printer listener through the
        # listener registry launcher, not a hard-coded domain module.
        for process in ("python3 -m print_handy.bot", "python3 -m print_handy.listeners",
                        "python3 -m print_handy.webui_gate", "python3 -m print_handy.webui",
                        "uvicorn spoolman.main:app"):
            with self.subTest(process=process):
                self.assertIn(process, supervisor)

    def test_runtime_installs_tools_required_by_shipped_shell_wrappers(self):
        """#236: moonrakerctl uploads with curl and parses with jq; missing tools broke native prints.

        The shipped shell wrappers declare their non-base external tools here. Every
        declared tool must actually be used by its wrapper (so the declaration cannot
        go stale) and must be installed by the runtime stage (so a wrapper gaining a
        new tool dependency without a Dockerfile update fails this test). Tools the
        wrappers use from the Debian base system (bash, coreutils, mawk as awk, tar,
        gzip and util-linux as flock) and python3/xvfb-run, which the runtime stage
        installs for other reasons, are not repeated here.
        """
        wrappers = {
            "bin/moonrakerctl": {"curl", "jq"},
            # Sourced by moonrakerctl for mesh-run.
            "bin/mesh-routine.sh": {"jq"},
        }
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime = dockerfile.split("AS runtime", 1)[1]
        install = re.search(r"apt-get install -y --no-install-recommends\s+(.*?)\s*&& rm -rf",
                            runtime, re.S)
        self.assertIsNotNone(install)
        packages = set(install.group(1).replace("\\", " ").split())
        for wrapper, tools in wrappers.items():
            text = (ROOT / wrapper).read_text()
            for tool in sorted(tools):
                with self.subTest(wrapper=wrapper, tool=tool):
                    self.assertRegex(text, rf"\b{tool}\b")
                    self.assertIn(tool, packages)

    def test_runtime_installs_ffmpeg_for_timelapse_delivery(self):
        """#373: ffmpeg is a runtime dependency of the shipped processes, not a build tool.

        The bot/listener processes size printer-rendered timelapses, assemble
        captured ones and build the vertical short with the ffmpeg the settings
        resolve from PATH (paths.ffmpeg). It must sit in the runtime stage's
        unconditional package list (every published variant carries it, like
        curl/jq in #236), the stage must run as the unprivileged runtime user,
        and the VAAPI driver stack the h264_vaapi encoder needs must ship in
        the same stage. The live half of this pin — the binary resolves and
        runs for the runtime user and lists libx264 + h264_vaapi — is the
        image probe's ffmpeg check, wired into the acceptance loop.
        """
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime = dockerfile.split("AS runtime", 1)[1]
        install = re.search(r"apt-get install -y --no-install-recommends\s+(.*?)\s*&& rm -rf",
                            runtime, re.S)
        self.assertIsNotNone(install)
        packages = set(install.group(1).replace("\\", " ").split())
        self.assertIn("ffmpeg", packages)
        self.assertIn("USER handy", runtime)
        for vaapi_package in ("intel-media-va-driver", "mesa-va-drivers", "libva-drm2"):
            with self.subTest(package=vaapi_package):
                self.assertIn(vaapi_package, runtime)
        probe = (ROOT / "tests" / "docker" / "image_probe.py").read_text()
        self.assertIn("def ffmpeg(", probe)
        self.assertIn('"ffmpeg": ffmpeg', probe)
        acceptance = (ROOT / "tests" / "docker" / "image_acceptance.sh").read_text()
        self.assertIn("payload closure slice egress ffmpeg openscad", acceptance)

    def test_runtime_installs_openscad_for_cad_split_and_design(self):
        """#388: OpenSCAD is a runtime dependency (CAD split, previews, and
        now the /print-design engine), resolved from PATH by the runtime
        user. The live half of this pin — the binary resolves, runs and
        reports its version — is the image probe's openscad check, wired
        into the acceptance loop."""
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime = dockerfile.split("AS runtime", 1)[1]
        install = re.search(r"apt-get install -y --no-install-recommends\s+(.*?)\s*&& rm -rf",
                            runtime, re.S)
        self.assertIsNotNone(install)
        packages = set(install.group(1).replace("\\", " ").split())
        self.assertIn("openscad", packages)
        probe = (ROOT / "tests" / "docker" / "image_probe.py").read_text()
        self.assertIn("def openscad(", probe)
        self.assertIn('"openscad": openscad', probe)

    def test_docker_examples_use_source_installed_slicer(self):
        for suffix in ("yaml", "toml"):
            with self.subTest(format=suffix):
                config = (ROOT / "config" / f"docker.{suffix}").read_text()
                self.assertIn("/usr/local/bin/orca-slicer", config)
                self.assertNotIn("/opt/orcaslicer/AppRun", config)

    def test_single_container_with_included_spoolman_api(self):
        """#214: one Compose service runs Print Handy plus the bundled Spoolman API on loopback."""
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        services = compose["services"]
        # One container runs Print Handy; the only other service is the
        # optional profile-gated cloudflared sidecar (#311), which runs no
        # Print Handy code and shares the combined network namespace.
        self.assertEqual(list(services), ["combined", "cloudflared"])
        self.assertEqual(services["cloudflared"]["profiles"], ["live-stream"])
        service = services["combined"]
        self.assertNotIn("command", service)
        # The one published port is the Web UI's, so a stock Compose
        # first boot can reach it; nothing else is published.
        self.assertEqual(service["ports"], ["8088:${PRINT_HANDY__WEBUI__PORT:-8088}"])
        self.assertNotIn("depends_on", service)
        self.assertTrue(service["init"])
        self.assertEqual(service["healthcheck"]["test"], ["CMD", "python3", "/usr/local/bin/print-handy-healthcheck"])
        self.assertFalse(any(v.split(":")[-1].startswith("/app") for v in service["volumes"]))
        self.assertTrue(any(v.endswith(":/data/spoolman") for v in service["volumes"]))
        env = service["environment"]
        # Bundled API by default; a non-empty external URL in .env wins and nothing is migrated.
        self.assertEqual(env["PRINT_HANDY__SERVICES__SPOOLMAN_URL"],
                         "${PRINT_HANDY__SERVICES__SPOOLMAN_URL:-http://127.0.0.1:8000}")
        self.assertEqual(env["SPOOLMAN_HOST"], "${SPOOLMAN_HOST:-127.0.0.1}")
        self.assertEqual(env["SPOOLMAN_DIR_DATA"], "/data/spoolman")
        # Spoolman's backend only: pinned revision, locked deps, no Node build or web UI assets.
        dockerfile = (ROOT / "Dockerfile").read_text()
        stage = dockerfile.split("AS spoolman-build", 1)[1].split("AS runtime", 1)[0]
        self.assertRegex(stage, r"ARG SPOOLMAN_REF=[0-9a-f]{40}\n")
        self.assertIn("uv sync --locked --no-dev", stage)
        self.assertIn("rm -rf .git client client_v2 && mkdir -p client_v2/build", stage)
        self.assertNotRegex(stage, r"\b(npm|node|vite)\b")
        self.assertIn("COPY --from=spoolman-build /opt/spoolman /opt/spoolman", dockerfile)

    def test_bundled_spoolman_update_check_is_disabled(self):
        """Spoolman 0.27.1's outbound update check stays off for the bundled instance.

        Upstream defaults SPOOLMAN_UPDATE_CHECK to true, which would make
        the bundled Spoolman poll api.github.com - wrong for a local-first
        product whose bundled copy is updated by Print Handy's own pins.
        The supervisor is the one launcher of the bundled instance (the
        Compose service and the Unraid template run this same container
        and never launch Spoolman themselves), so its exported default
        is the contract; the operator surfaces document the same value.
        """
        supervisor = (ROOT / "docker" / "supervise.sh").read_text()
        self.assertIn('export SPOOLMAN_UPDATE_CHECK="${SPOOLMAN_UPDATE_CHECK:-FALSE}"',
                      supervisor)
        # The export must land before the bundled launch uses the env.
        self.assertLess(supervisor.index("SPOOLMAN_UPDATE_CHECK"),
                        supervisor.index("uvicorn spoolman.main:app"))
        env_example = (ROOT / ".env.example").read_text()
        self.assertIn("# SPOOLMAN_UPDATE_CHECK=FALSE", env_example)
        settings = (ROOT / "docs" / "SETTINGS.md").read_text()
        self.assertIn("| `SPOOLMAN_UPDATE_CHECK` | `FALSE` |", settings)

    def test_spoolman_stub_page_points_at_the_api_and_the_web_ui_spools_page(self):
        """#940: the bundled Spoolman's root serves a stub page, not a web UI.

        A browser (or a dashboard webpage card, for example in Home
        Assistant) that opens the bundled Spoolman's root gets the
        200.html the spoolman-build stage writes. That page must not be a
        dead end: it keeps the managed-by sentence, says the bundled
        Spoolman has no web UI and is API-only, links to the API, and
        links onward to the Print Handy Web UI Spools page. The stub is
        served from the Spoolman port while the Web UI runs on its own
        port (default 8088), so the onward link is built from the page's
        own protocol and host plus the Web UI port and the /spools path,
        with visible fallback text spelling out that pattern.
        """
        dockerfile = (ROOT / "Dockerfile").read_text()
        stage = dockerfile.split("AS spoolman-build", 1)[1].split("AS runtime", 1)[0]
        self.assertIn("client_v2/build/200.html", stage)
        # (a) The managed-by sentence survives.
        self.assertIn("Spoolman is managed by Print Handy", stage)
        # (d) The page says the bundled Spoolman has no web UI / is API-only.
        self.assertIn("has no web UI", stage)
        self.assertIn("API-only", stage)
        # (b) The page links to the Spoolman API.
        self.assertIn('href="/api/v1"', stage)
        # (c) The onward link targets the Web UI Spools page: same protocol
        # and host as the stub page, Web UI port (default 8088), /spools.
        self.assertIn("location.protocol", stage)
        self.assertIn("location.hostname", stage)
        self.assertIn(":8088/spools", stage)
        self.assertIn("Web UI port (default 8088)", stage)
        self.assertIn("Web UI must be enabled", stage)
        # Still one self-contained page: no copied frontend assets.
        self.assertNotIn("client_v2/build/assets", stage)

    def test_configuration_parity_channels_and_packaged_plugins(self):
        import tomllib
        from print_handy import settings

        configs = [settings.load(local=ROOT / "config" / f"docker.{suffix}", environ={})
                   for suffix in ("yaml", "toml")]
        self.assertEqual(configs[0], configs[1])
        self.assertEqual(configs[0]["services"]["spoolman_url"], "http://127.0.0.1:8000")
        self.assertEqual(configs[0]["orca"]["command"], ["/usr/local/bin/orca-slicer"])
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("ARG APP_CHANNEL=development", dockerfile)
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertNotIn("plugins", ignored)
        self.assertIn(".env", ignored)
        self.assertTrue((ROOT / "plugins" / "print-feedback").is_dir())
        # The old opt-in override file is gone: Spoolman lives in the base file (#214).
        self.assertFalse((ROOT / "docker-compose.spoolman.yml").exists())

    # #971: runs the shipped image's entrypoint, so it is container-marked
    # (runs in the image and in the CI offline-tests job; skips locally).
    @pytest.mark.container
    def test_entrypoint_selects_the_single_stable_build_and_aliases_the_removed_preview(self):
        script = ROOT / "docker" / "entrypoint.sh"
        with tempfile.TemporaryDirectory(dir=ROOT / ".") as data:
            Path(data, "hermes/state").mkdir(parents=True)
            Path(data, "archive").mkdir()
            Path(data, "records").mkdir()
            Path(data, "cache").mkdir()
            command = ["sh", "-c", "printf \"%s\\n\" \"$PRINT_HANDY_ORCA_CMD\""]
            # The preview channel was removed (one bundled Orca only):
            # 'preview' still starts, on the single stable build, with a
            # startup notice saying the channel was removed.
            env = {"PATH": os.environ["PATH"], "HOME": data, "PRINT_HANDY_ORCA_CHANNEL": "preview"}
            preview = subprocess.run(["bash", str(script), *command], cwd=data, env=env,
                                     text=True, capture_output=True, check=True)
            self.assertEqual(preview.stdout.strip(), "/usr/local/bin/orca-slicer-stable")
            self.assertIn("the preview channel was removed", preview.stderr)
            self.assertIn("setting can be dropped", preview.stderr)
            # 'alpha', the preview channel's pre-#600 name, gets the same
            # treatment: the single stable build plus the removal notice.
            env["PRINT_HANDY_ORCA_CHANNEL"] = "alpha"
            legacy = subprocess.run(["bash", str(script), *command], cwd=data, env=env,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(legacy.stdout.strip(), "/usr/local/bin/orca-slicer-stable")
            self.assertIn("the preview channel was removed", legacy.stderr)
            self.assertIn("PRINT_HANDY_ORCA_CHANNEL=stable", legacy.stderr)
            env.pop("PRINT_HANDY_ORCA_CHANNEL")
            stable = subprocess.run(["bash", str(script), *command], cwd=data, env=env,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(stable.stdout.strip(), "/usr/local/bin/orca-slicer-stable")
            # #972: a container-level PRINT_HANDY_ORCA_CMD override wins
            # over the entrypoint default (the image ENV sets the same
            # stable default, so this also covers docker exec inheriting
            # an explicitly set container value).
            env["PRINT_HANDY_ORCA_CMD"] = "/custom/orca-slicer"
            override = subprocess.run(["bash", str(script), *command], cwd=data, env=env,
                                      text=True, capture_output=True, check=True)
            self.assertEqual(override.stdout.strip(), "/custom/orca-slicer")
            env.pop("PRINT_HANDY_ORCA_CMD")
            env["PRINT_HANDY_ORCA_CHANNEL"] = "other"
            invalid = subprocess.run(["bash", str(script), *command], cwd=data, env=env,
                                     text=True, capture_output=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("must be 'stable'", invalid.stderr)

    def test_image_env_sets_orca_cmd_for_docker_exec(self):
        """#972: PRINT_HANDY_ORCA_CMD is set in the image ENV, not only in
        the bot's process env.

        `docker exec <container> ... model_job print-now` starts a fresh
        process that inherits the image/container environment, not the
        entrypoint's exports to its own children. If the variable lives
        only in the entrypoint export, the exec'd process recomputes the
        slice recipe with a different slicer command and print-now refuses
        with "stale color artifact". The runtime stage must therefore carry
        the exact ENV line, and the entrypoint must default (not override)
        the variable so a container-level override still wins.
        """
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime = dockerfile.split("AS runtime", 1)[1]
        self.assertIn("ENV PRINT_HANDY_ORCA_CMD=/usr/local/bin/orca-slicer-stable", runtime)
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
        self.assertIn(': "${PRINT_HANDY_ORCA_CMD:=/usr/local/bin/orca-slicer-stable}"', entrypoint)
        self.assertNotIn('export PRINT_HANDY_ORCA_CMD="$selected"', entrypoint)

    def test_orca_is_optional_and_prebuilt_only(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("ARG WITH_ORCA=", dockerfile)
        self.assertNotIn("ARG ORCA_CHANNEL=", dockerfile)
        self.assertNotIn("ORCA_SOURCE", dockerfile)
        self.assertNotIn("AS orca-stable", dockerfile)
        self.assertNotIn("AS orca-alpha", dockerfile)
        self.assertFalse((ROOT / "docker" / "orca-build.sh").exists())
        self.assertIn('COPY --from=orca-prebuilt /out/ /', dockerfile)
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
        self.assertIn("PRINT_HANDY_ORCA_CHANNEL:-stable", entrypoint)
        self.assertIn(': "${PRINT_HANDY_ORCA_CMD:=/usr/local/bin/orca-slicer-stable}"', entrypoint)
        self.assertIn("export PRINT_HANDY_ORCA_CMD", entrypoint)
        self.assertIn("must be 'stable'", entrypoint)
        runtime = dockerfile.split("AS runtime", 1)[1]
        self.assertIn("openscad", runtime)
        self.assertIn("--no-install-recommends", runtime)
        prebuilt = (ROOT / "docker" / "orca-prebuilt.sh").read_text()
        for pin in ("OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage",
                    "d13eb0a9cd618ed080ab6b82c64c0a60c4501aac91e88d5ac2c3b526fb141a7c",
                    "40ecc66f78ed72dc368e0c1ebe40d77f00cc8a19",
                    "OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0.AppImage",
                    "00a4fa0a2f9c1649abb8c438b0537ed1e58fed5206faa560c1c0fb4ab225f9cf"):
            with self.subTest(pin=pin):
                self.assertIn(pin, prebuilt)
        self.assertIn("sha256sum --check --strict -", prebuilt)
        self.assertLess(prebuilt.index("sha256sum --check"), prebuilt.index("--appimage-extract"))
        self.assertIn("set -eu", prebuilt)
        self.assertIn("--appimage-extract", prebuilt)
        self.assertNotIn("--appimage-extract-and-run", prebuilt)
        self.assertIn('mv "squashfs-root" "/out/opt/orca-$channel"', prebuilt)
        self.assertIn("exec /opt/orca-%s/AppRun", prebuilt)
        self.assertIn("libopengl0 libglu1-mesa", runtime)

    def test_bambu_studio_is_optional_pinned_and_built_from_source(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        stage = dockerfile.split("AS bambu-build", 1)[1].split("AS runtime", 1)[0]
        self.assertIn("ARG WITH_BAMBU=false", stage)
        self.assertRegex(stage, r"ARG BAMBU_REF=[0-9a-f]{40}\n")
        self.assertIn("https://github.com/bambulab/BambuStudio.git", stage)
        self.assertIn('test "$(git rev-parse HEAD)" = "$ref"', stage)
        self.assertIn("-DSLIC3R_FHS=ON", stage)
        self.assertNotIn(".AppImage", stage)
        self.assertIn("COPY --from=bambu-build /out/ /", dockerfile)
        for suffix in ("yaml", "toml"):
            self.assertIn("/usr/bin/bambu-studio", (ROOT / "config" / f"docker.{suffix}").read_text())

    def test_documentation_and_app_version_match_the_docker_contract(self):
        docs = (ROOT / "docs" / "DOCKER.md").read_text()
        self.assertIn("PRINT_HANDY_ORCA_CHANNEL", docs)
        self.assertIn("OpenSCAD (`openscad`) is included", docs)
        self.assertNotIn("KiCad", docs)
        self.assertIn("No Node, npm or web UI assets are built or copied", docs)
        self.assertIn("Default bind is `127.0.0.1:8000` inside the container", docs)

    def test_validation_workflow_records_prebuilt_orca_pins(self):
        workflow = (ROOT / ".github" / "workflows" / "docker-image-validation.yml").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("      orca_channel:", workflow)
        self.assertNotIn("inputs.orca_channel", workflow)
        self.assertNotIn("ORCA_CHANNEL:", workflow)
        self.assertNotIn("ORCA_STABLE_REF", workflow)
        self.assertNotIn("ORCA_ALPHA_REF", workflow)
        self.assertNotIn("ORCA_SOURCE", workflow)
        self.assertIn("docker/orca-prebuilt.sh", workflow)
        self.assertIn("for channel in stable; do", workflow)
        self.assertNotIn("orca-preview", workflow)
        self.assertIn("AS orca-prebuilt", dockerfile)
        # The pin counts match the per-arch pin structure of the one bundled
        # build: one revision; SHA256 digests and asset names are per arch (2 each).
        self.assertIn("grep -cE 'sha256_(amd64|arm64)=[0-9a-f]{64}' docker/orca-prebuilt.sh", workflow)
        self.assertIn("grep -cE 'asset_(amd64|arm64)=' docker/orca-prebuilt.sh", workflow)

    def test_validation_workflow_offers_both_arches_on_native_runners(self):
        workflow = yaml.load((ROOT / ".github" / "workflows" / "docker-image-validation.yml").read_text(),
                             Loader=yaml.BaseLoader)
        self.assertEqual(list(workflow["on"]), ["workflow_dispatch"])
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["architecture"]["options"], ["amd64", "arm64"])
        self.assertEqual(inputs["architecture"]["default"], "amd64")
        # One image is published (#353): there is no variant input any more.
        self.assertNotIn("variant", inputs)
        job = workflow["jobs"]["validate"]
        # arm64 validates on the native ARM runner, amd64 on the x86 runner.
        self.assertIn("ubuntu-24.04-arm", job["runs-on"])
        self.assertIn("ubuntu-24.04", job["runs-on"])
        self.assertIn("inputs.architecture", job["runs-on"])
        env = job["env"]
        self.assertIn("linux/arm64", env["PLATFORM"])
        self.assertIn("linux/amd64", env["PLATFORM"])
        # The published image's toggles are fixed: Orca and go2rtc in, Bambu out.
        self.assertEqual(env["WITH_ORCA"], "true")
        self.assertEqual(env["WITH_GO2RTC"], "true")
        self.assertEqual(env["WITH_BAMBU"], "false")
        build = next(step for step in job["steps"] if step.get("name") == "Build (no push)")
        self.assertIn('--platform "$PLATFORM"', build["run"])
        self.assertIn("image_acceptance.sh", job["steps"][-3]["run"])
        self.assertIn("lifecycle_gate.sh", job["steps"][-2]["run"])
        smoke = next(step for step in job["steps"]
                     if step.get("name") == "Headless slice with the bundled Orca")
        # The published image always bundles Orca, so the smoke is unconditional.
        self.assertNotIn("if", smoke)
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")

    def test_publish_workflow_has_no_orca_revision_selector(self):
        publish_text = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()
        self.assertNotIn("orca_channel", publish_text)

    def test_ghcr_publisher_is_manual_gated_and_uses_pinned_actions(self):
        workflow = yaml.load((ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(),
                             Loader=yaml.BaseLoader)
        self.assertEqual(list(workflow["on"]), ["workflow_dispatch"])
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["version"]["required"], "true")
        self.assertNotIn("orca_source", inputs)
        # The published image's toggles are fixed in the workflow (Orca and
        # go2rtc in, WITH_BAMBU=false), not dispatch inputs.
        self.assertNotIn("with_orca", inputs)
        self.assertNotIn("with_bambu", inputs)
        # Candidate validation must not authenticate or push without explicit opt-in.
        self.assertEqual(inputs["publish"]["type"], "boolean")
        self.assertEqual(inputs["publish"]["default"], "false")
        steps = workflow["jobs"]["build"]["steps"]
        acceptance = next(i for i, step in enumerate(steps) if step.get("id") == "acceptance")
        publish = next(i for i, step in enumerate(steps) if step.get("id") == "publish")
        self.assertLess(acceptance, publish)
        lifecycle = next(i for i, step in enumerate(steps) if step.get("id") == "lifecycle")
        self.assertLess(acceptance, lifecycle)
        self.assertLess(lifecycle, publish)
        self.assertIn("lifecycle_gate.sh", steps[lifecycle]["run"])
        self.assertNotIn("docker push", steps[lifecycle]["run"])
        publish_gate = ("inputs.publish && steps.acceptance.outcome == 'success'"
                        " && steps.lifecycle.outcome == 'success'")
        self.assertEqual(steps[publish]["if"], publish_gate)
        login = next(step for step in steps
                     if step.get("uses", "").startswith("docker/login-action@"))
        self.assertEqual(login["if"], publish_gate)
        # Build legs push by digest only; no tag is ever pushed from a leg.
        self.assertIn("push-by-digest=true", steps[publish]["run"])
        self.assertNotIn("docker push", steps[publish]["run"])
        self.assertNotIn("docker tag", steps[publish]["run"])
        self.assertIn("image_acceptance.sh", steps[acceptance]["run"])
        smoke = next(step for step in steps if step.get("name") == "Headless slice with the bundled Orca")
        # The published image always bundles Orca, so the smoke is unconditional.
        self.assertNotIn("if", smoke)
        self.assertIn('channels="stable"', smoke["run"])
        self.assertNotIn("preview", smoke["run"])
        self.assertNotIn("ORCA_SOURCE", smoke["run"])
        self.assertIn("--network none", smoke["run"])
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"^[^@]+@[0-9a-f]{40}$")

    def test_publish_context_excludes_generated_evidence(self):
        """P2-87: the publish workflows write evidence/ into the checkout
        (pins, identity, slicer logs, the acceptance tree) before the push
        build runs, and the Dockerfile copies the whole context — so unless
        evidence/ is dockerignored, the rebuild packages those records into
        the image and what ships is not the image acceptance ran against."""
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertIn("evidence", [line.strip().rstrip("/") for line in ignored])

    def test_publish_step_verifies_the_pushed_image_is_the_accepted_one(self):
        """P2-87: pushing re-runs the build, so before the pushed digest may
        be handed to the manifest job the step proves the pushed manifest's
        config digest equals the accepted local image's ID, and records both
        in the evidence. The proof must precede the digest hand-off: a
        mismatch stops the leg instead of publishing an untested image."""
        workflow = yaml.load((ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(),
                             Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["build"]["steps"]
        publish = next(step for step in steps if step.get("id") == "publish")
        run = publish["run"]
        self.assertIn("docker image inspect", run)
        self.assertIn('"$LOCAL_IMAGE"', run)
        self.assertIn("pushed_image_ids.py", run)
        self.assertIn("pushed_image_ids.py", run)
        self.assertIn("evidence/identity-check.txt", run)
        self.assertLess(run.index("pushed_image_ids.py"), run.index("digest-out"))

    def test_publish_matrix_is_native_both_arches_full_image_only(self):
        """#192/#353: linux/amd64 + linux/arm64, one full image, native runners, no QEMU, no arm/v7."""
        workflow = yaml.load((ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(),
                             Loader=yaml.BaseLoader)
        text = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()
        job = workflow["jobs"]["build"]
        self.assertEqual(job["runs-on"], "${{ matrix.runner }}")
        self.assertEqual(job["timeout-minutes"], "350")  # BaseLoader: scalars are strings
        legs = {leg["arch"]: leg for leg in job["strategy"]["matrix"]["include"]}
        self.assertEqual(set(legs), {"amd64", "arm64"})
        runners = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
        for arch, leg in legs.items():
            with self.subTest(arch=arch):
                self.assertEqual(leg["runner"], runners[arch])
                self.assertEqual(leg["platform"], f"linux/{arch}")
                self.assertNotIn("variant", leg)
        # The image toggles are fixed at job level: Orca and go2rtc in, Bambu out.
        self.assertEqual(job["env"]["WITH_ORCA"], "true")
        self.assertEqual(job["env"]["WITH_GO2RTC"], "true")
        self.assertEqual(job["env"]["WITH_BAMBU"], "false")
        self.assertNotIn("arm/v7", text)
        self.assertNotIn("setup-qemu", text.lower())
        self.assertNotIn("-light", text)
        build = next(step for step in job["steps"] if step.get("name") == "Build image once (no push)")
        self.assertIn('--platform "$PLATFORM"', build["run"])
        self.assertIn('--load -t "$LOCAL_IMAGE"', build["run"])

    def test_publish_manifest_job_combines_accepted_digests_into_the_two_tags(self):
        workflow = yaml.load((ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(),
                             Loader=yaml.BaseLoader)
        manifest = workflow["jobs"]["manifest"]
        # Publication only, gated on every build leg (each leg is itself
        # acceptance-gated before its digest push).
        self.assertEqual(manifest["needs"], "build")
        self.assertEqual(manifest["if"], "inputs.publish")
        create = next(step for step in manifest["steps"]
                      if step.get("name") == "Create and push the multi-arch manifests")
        run = create["run"]
        self.assertIn("docker buildx imagetools create", run)
        for tag in ('"$IMAGE:$PUBLISH_VERSION"', '"$IMAGE:latest"'):
            with self.subTest(tag=tag):
                self.assertIn(f"-t {tag}", run)
        for digest_var in ("full_amd64", "full_arm64"):
            with self.subTest(digest=digest_var):
                self.assertIn(f'"$IMAGE@${digest_var}"', run)
        self.assertNotIn("-light", run)
        # Manifest digests land in the step summary and the evidence.
        self.assertIn("GITHUB_STEP_SUMMARY", run)
        self.assertIn("evidence/manifests.txt", run)
        download = manifest["steps"][0]
        self.assertTrue(download["uses"].startswith("actions/download-artifact@"))
        self.assertEqual(download["with"]["pattern"], "digest-*")

    def test_no_light_variant_and_no_orca_build_still_fails_closed(self):
        """#353: no workflow publishes or validates a -light image any more.

        A locally built WITH_ORCA=false image (a build option, not a shipped
        variant) keeps the existing no-Orca behaviour: non-slicing commands
        run, slicing fails closed (tests/docker/image_probe.py proves no
        G-code is written).
        """
        for name in ("docker-publish.yml", "nightly.yml", "docker-image-validation.yml"):
            with self.subTest(workflow=name):
                text = (ROOT / ".github" / "workflows" / name).read_text()
                self.assertNotIn("-light", text)
                self.assertNotIn("variant", text)
        docs = (ROOT / "docs" / "DOCKER.md").read_text()
        self.assertIn("WITH_ORCA=false", docs)
        self.assertIn("WITH_BAMBU=false", docs)
        probe = (ROOT / "tests" / "docker" / "image_probe.py").read_text()
        self.assertIn("FileNotFoundError", probe)
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("openscad", dockerfile.split("AS runtime", 1)[1])

    def test_no_global_ld_library_path_into_orca_runtime(self):
        """2.1.5 release blocker: a global LD_LIBRARY_PATH into Orca's bundled
        runtime makes the loader resolve the system ffmpeg's libav* from
        Orca 2.5's bundled FFmpeg 7.0 copies (identical sonames), killing
        ffmpeg before it prints anything — the candidate image's ffmpeg
        probe found the binary but no version output and no encoders. The
        path stays scoped to Orca invocations: upstream's AppRun env helper
        sets it for wrapped launches, and the raw-binary checks
        (image_probe.py, slicer-smoke.py) set it per invocation."""
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIsNone(re.search(r"^\s*ENV\s+[^#]*LD_LIBRARY_PATH", dockerfile, re.M),
                          "Dockerfile must not set LD_LIBRARY_PATH as a global ENV")
        probe = (ROOT / "tests" / "docker" / "image_probe.py").read_text()
        self.assertIn("def _orca_env(", probe)
        self.assertIn("/opt/orca-stable/lib/orca-runtime", probe)
        self.assertGreaterEqual(probe.count("env=env"), 2)  # ldd and --help both scoped
        smoke = (ROOT / "docker" / "slicer-smoke.py").read_text()
        self.assertIn('f"/opt/orca-{channel}/lib/orca-runtime"', smoke)

    def test_intel_vaapi_driver_is_amd64_only(self):
        """2.1.5 release blocker: intel-media-va-driver has no arm64
        candidate in Debian trixie, so the unconditional install killed the
        arm64 image build outright. It installs only behind a TARGETARCH
        amd64 check; the rest of the VAAPI stack ships on both arches."""
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime = dockerfile.split("AS runtime", 1)[1]
        self.assertIn("ARG TARGETARCH", runtime)
        code = "\n".join(line for line in runtime.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(code.count("intel-media-va-driver"), 1)
        self.assertRegex(runtime, r'if \[ "\$TARGETARCH" = amd64 \]; then \\\n'
                                  r'\s+apt-get install -y --no-install-recommends intel-media-va-driver'
                                  r' libmfx-gen1\.2;')
        unconditional = re.search(r"apt-get install -y --no-install-recommends \\\n"
                                  r"\s+mesa-va-drivers libva-drm2 vainfo", runtime)
        self.assertIsNotNone(unconditional)

    def test_gpu_encoder_runtimes_ship_for_qsv_and_nvenc(self):
        """#455: the encoders ffmpeg lists must be able to open on a granted GPU.

        h264_qsv needs the oneVPL GPU runtime (libmfx-gen1.2, amd64-only like
        the Intel VA driver, so it sits in the same amd64 branch), and
        h264_nvenc needs the NVIDIA runtime to mount libnvidia-encode, which
        it does only when NVIDIA_DRIVER_CAPABILITIES names "video". Neither
        selects or requires a GPU: without one, both are inert.
        """
        dockerfile = (ROOT / "Dockerfile").read_text()
        runtime = dockerfile.split("AS runtime", 1)[1]
        code = "\n".join(line for line in runtime.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(code.count("libmfx-gen1.2"), 1)
        capabilities = re.search(r"(?m)^ENV NVIDIA_DRIVER_CAPABILITIES=(\S+)$", runtime)
        self.assertIsNotNone(capabilities)
        self.assertIn("video", capabilities.group(1).split(","))
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", code)  # the operator picks the GPU

    def test_probe_runs_orca_from_a_writable_cwd(self):
        """2.1.5 release blocker: Orca writes 00000.log into its working
        directory at startup and aborts when it cannot; the probe ran its
        Orca subprocesses with no cwd, inheriting the root-owned /app.
        Every Orca invocation in the probe now passes the writable work
        directory it creates under the cache mount."""
        probe = (ROOT / "tests" / "docker" / "image_probe.py").read_text()
        self.assertGreaterEqual(probe.count("cwd=work"), 2)  # closure --help and the slice run
        self.assertNotIn('subprocess.run([str(ORCA), "--help"], capture_output=True, text=True, timeout=300)',
                         probe)
        self.assertNotIn('run = subprocess.run(command, capture_output=True, text=True, timeout=900)',
                         probe)

    def test_acceptance_polls_the_per_printer_heartbeat(self):
        """2.1.5 release blocker: since the Stage 2b listener rework (#334)
        the heartbeat lives at <state>/printers/<id>/listener_health.json;
        the acceptance script kept polling the legacy flat file and reported
        'never connected' while the listener was connected."""
        acceptance = (ROOT / "tests" / "docker" / "image_acceptance.sh").read_text()
        self.assertIn("/data/state/state/printers/*/listener_health.json", acceptance)
        self.assertNotIn("/data/state/state/listener_health.json", acceptance)


if __name__ == "__main__":
    unittest.main()
