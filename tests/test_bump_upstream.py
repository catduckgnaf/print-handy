"""Offline tests for scripts/bump_upstream.py and its two workflows.

Phase A of the upstream bump automation: the script plans and opens
draft bump PRs for go2rtc and Spoolman, and flips them to ready
under conservative guards. Phase B adds the Bambu Studio planner:
a commit pin compared by dates, the recorded-surface sweep, and
HMS table regeneration from a fixture Bambu Studio tree. Phase C
adds the OrcaSlicer planner: a single-channel pin compared by
prerelease-aware version ordering, the recorded-surface sweep, and
the identity sweep that verifies a new bundle's vendor profiles
against the recorded identities (the sweep-driver tests run the
repo's real flatten pipeline against a synthetic mini-vendor tree,
and the bundle-fetch test extracts a fake AppImage script).
Everything here runs on fixtures - no network, no real git (the
HMS tests do run the repo's real generator script, in a staged
temp layout).
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bump_upstream.py"
BUMP_WORKFLOW = ROOT / ".github" / "workflows" / "upstream-bump.yml"
READY_WORKFLOW = ROOT / ".github" / "workflows" / "upstream-bump-ready.yml"

spec = importlib.util.spec_from_file_location("bump_upstream", SCRIPT)
bump = importlib.util.module_from_spec(spec)
sys.modules["bump_upstream"] = bump  # dataclasses resolve the module by name
spec.loader.exec_module(bump)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

# Shape-valid private key fixture: header/footer only; not a real key.
VALID_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "ZmFrZS1rZXktbWF0ZXJpYWw=\n"
    "-----END RSA PRIVATE KEY-----\n"
)

BOT_ENV = {"UPSTREAM_BOT_APP_ID": "123456", "UPSTREAM_BOT_PRIVATE_KEY": VALID_KEY}

# Frozen fixture copies of the pin surfaces (tests/fixtures/bump_upstream).
# Parser and planner tests run against these, never against the live
# production pins: every bump PR changes the production files, so a
# test that hardcoded the live values would fail the bump's own CI.
FIXTURES = ROOT / "tests" / "fixtures" / "bump_upstream"
GO2RTC_TEXT = (FIXTURES / "docker" / "go2rtc-prebuilt.sh").read_text()
DOCKERFILE_TEXT = (FIXTURES / "Dockerfile").read_text()
ORCA_TEXT = (FIXTURES / "docker" / "orca-prebuilt.sh").read_text()


def fixture_root(base):
    """Copy the fixture tree into a temp root with production layout.

    A trailing .txt on a fixture name is stripped on the way out: the
    guard-test excerpt is a fragment of a Python file, stored as
    .py.txt so neither the whole-tree linter nor test collection
    mistakes the fragment for a module, and the copied HMS table is
    stored as .json.txt so repo-wide shipped-file scans never see
    its production name."""
    import shutil

    root = Path(base)
    for src in FIXTURES.rglob("*"):
        if src.is_file():
            rel = src.relative_to(FIXTURES)
            if rel.name.endswith(".txt"):
                rel = rel.with_name(rel.name[: -len(".txt")])
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
    return root


def make_release(version, age_days=10, assets=None, tag=None):
    return bump.Release(
        tag=tag or f"v{version}",
        version=version,
        name=f"v{version}",
        published_at=NOW - timedelta(days=age_days),
        html_url=f"https://example.test/releases/{version}",
        assets=assets or [],
    )


def asset(name, digest):
    return {"name": name, "digest": digest, "browser_download_url": f"https://example.test/dl/{name}"}


def make_orca_release(version, age_days=10, prerelease=False, assets=None, tag=None):
    release = make_release(version, age_days=age_days, assets=assets, tag=tag)
    release.prerelease = prerelease
    return release


# The Orca asset names a bump to a version derives from the fixture pin's.
def orca_assets(version, digests=("3" * 64, "4" * 64, "5" * 64)):
    return [
        asset(f"OrcaSlicer_Linux_AppImage_Ubuntu2404_V{version}.AppImage", "sha256:" + digests[0]),
        asset(f"OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V{version}.AppImage", "sha256:" + digests[1]),
        asset(f"OrcaSlicer-Linux-flatpak_V{version}_x86_64.flatpak", "sha256:" + digests[2]),
    ]


class FakeApi:
    """Records calls; serves prepared releases, tag commits and PRs."""

    def __init__(self, releases=None, commits=None, compare="ahead", prs=None, download_data=b"binary", commit_dates=None, release_lists=None):
        self.releases = releases or {}
        self.commits = commits or {}
        self.compare = compare
        self.prs = prs or []
        self.download_data = download_data
        self.commit_dates = commit_dates or {}
        self.release_lists = release_lists or {}
        self.created = []
        self.labeled = []
        self.comments = []
        self.closed = []
        self.marked_ready = []
        self.checks = {}

    def get_latest_release(self, repo):
        return self.releases.get(repo)

    def list_releases(self, repo):
        return list(self.release_lists.get(repo, []))

    def resolve_tag_commit(self, repo, tag):
        return self.commits[(repo, tag)]

    def get_commit_date(self, repo, ref):
        return self.commit_dates[(repo, ref)]

    def compare_status(self, repo, base, head):
        return self.compare

    def list_open_prs(self, repo):
        return list(self.prs)

    def find_open_pr_for_branch(self, repo, branch):
        for pr in self.prs:
            if (pr.get("head") or {}).get("ref") == branch:
                return pr
        return None

    def download(self, url):
        return self.download_data

    def create_pr(self, repo, title, head, base, body):
        self.created.append({"title": title, "head": head, "base": base, "body": body})
        return {"number": 4242}

    def add_labels(self, repo, number, labels):
        self.labeled.append((number, labels))

    def comment(self, repo, number, body):
        self.comments.append((number, body))

    def close_pr(self, repo, number):
        self.closed.append(number)

    def check_runs_for_ref(self, repo, ref):
        return self.checks.get(ref, [])

    def mark_ready(self, node_id):
        self.marked_ready.append(node_id)


class FakeGit:
    def __init__(self):
        self.calls = []

    def current_ref(self):
        return "main"

    def create_branch(self, branch):
        self.calls.append(("branch", branch))

    def commit_files(self, files, message):
        self.calls.append(("commit", tuple(files), message))

    def push(self, branch):
        self.calls.append(("push", branch))

    def switch(self, ref):
        self.calls.append(("switch", ref))


def pr_dict(number, branch, labels=("upstream-bump",), draft=True, author="print-handy-upstream-bot[bot]", sha="a" * 40):
    return {
        "number": number,
        "draft": draft,
        "labels": [{"name": name} for name in labels],
        "user": {"login": author},
        "head": {"ref": branch, "sha": sha},
        "node_id": f"PR_node_{number}",
    }


class VersionOrdering(unittest.TestCase):
    def test_final_beats_prereleases(self):
        self.assertTrue(bump.is_newer("2.5.0", "2.5.0-rc"))
        self.assertTrue(bump.is_newer("2.5.0-rc", "2.5.0-beta"))
        self.assertTrue(bump.is_newer("2.5.0-beta", "2.5.0-alpha"))
        self.assertFalse(bump.is_newer("2.5.0-rc", "2.5.0"))

    def test_numeric_segments(self):
        self.assertTrue(bump.is_newer("1.9.16", "1.9.15"))
        self.assertTrue(bump.is_newer("0.27.1", "0.27.0"))
        self.assertFalse(bump.is_newer("1.9.15", "1.9.15"))
        self.assertFalse(bump.is_newer("1.9.9", "1.9.15"))

    def test_v_prefix_and_rc_numbers(self):
        self.assertTrue(bump.is_newer("v1.10.0", "1.9.99"))
        self.assertTrue(bump.is_newer("2.5.0-rc2", "2.5.0-rc1"))


class PinSurfaces(unittest.TestCase):
    """Against the frozen fixtures; the values below are the fixture's,
    frozen on purpose so future production bumps cannot break them."""

    def test_go2rtc_parse_fixture(self):
        pin = bump.parse_go2rtc(GO2RTC_TEXT)
        self.assertEqual(pin.version, "1.9.15")
        self.assertEqual(pin.revision, "7c05066")
        self.assertEqual(len(pin.sha256_amd64), 64)
        self.assertEqual(pin.asset_amd64, "go2rtc_linux_amd64")
        self.assertEqual(pin.asset_arm64, "go2rtc_linux_arm64")

    def test_go2rtc_round_trip(self):
        pin = bump.parse_go2rtc(GO2RTC_TEXT)
        pin.version = "1.9.16"
        pin.revision = "abc1234"
        out = bump.render_go2rtc(GO2RTC_TEXT, pin)
        again = bump.parse_go2rtc(out)
        self.assertEqual(again.version, "1.9.16")
        self.assertEqual(again.revision, "abc1234")
        # Nothing else in the file moved.
        self.assertEqual(out.replace("version=1.9.16", "version=1.9.15").replace("revision=abc1234", "revision=7c05066"), GO2RTC_TEXT)

    def test_spoolman_parse_and_round_trip(self):
        ref = bump.parse_spoolman(DOCKERFILE_TEXT)
        self.assertEqual(ref, "bed3f14ee77a3a571d31b14215d54b0b88a7519c")
        new_ref = "f" * 40
        out = bump.render_spoolman(DOCKERFILE_TEXT, new_ref)
        self.assertEqual(bump.parse_spoolman(out), new_ref)
        self.assertEqual(out.count(new_ref), 1)

    def test_bambu_parse_and_round_trip(self):
        ref = bump.parse_bambu(DOCKERFILE_TEXT)
        self.assertEqual(ref, "926a7192574bcb9b3a732e1ec59a46d79cb45466")
        new_ref = "d" * 40
        out = bump.render_bambu(DOCKERFILE_TEXT, new_ref)
        self.assertEqual(bump.parse_bambu(out), new_ref)
        # The Spoolman pin in the same file is untouched.
        self.assertEqual(bump.parse_spoolman(out), "bed3f14ee77a3a571d31b14215d54b0b88a7519c")

    def test_missing_pin_is_an_error(self):
        with self.assertRaises(bump.BumpError):
            bump.parse_go2rtc("version=1.0\n")

    def test_live_pin_files_keep_the_expected_shape(self):
        # Format-only assertions over the PRODUCTION files: whatever
        # values future bumps land, the shapes the parsers rely on
        # must hold. No live value is hardcoded here.
        pin = bump.parse_go2rtc((ROOT / "docker" / "go2rtc-prebuilt.sh").read_text())
        self.assertRegex(pin.version, r"^\d+\.\d+\.\d+$")
        self.assertRegex(pin.revision, r"^[0-9a-f]{7}$")
        self.assertRegex(pin.sha256_amd64, r"^[0-9a-f]{64}$")
        self.assertRegex(pin.sha256_arm64, r"^[0-9a-f]{64}$")
        self.assertTrue(pin.asset_amd64 and pin.asset_arm64)
        ref = bump.parse_spoolman((ROOT / "Dockerfile").read_text())
        self.assertRegex(ref, r"^[0-9a-f]{40}$")
        bambu_ref = bump.parse_bambu((ROOT / "Dockerfile").read_text())
        self.assertRegex(bambu_ref, r"^[0-9a-f]{40}$")
        orca_pin = bump.parse_orca((ROOT / "docker" / "orca-prebuilt.sh").read_text())
        self.assertRegex(orca_pin.version, r"^\d+\.\d+\.\d+(-[A-Za-z]+\d*)?$")
        self.assertRegex(orca_pin.revision, r"^[0-9a-f]{40}$")
        self.assertRegex(orca_pin.sha256_amd64, r"^[0-9a-f]{64}$")
        self.assertRegex(orca_pin.sha256_arm64, r"^[0-9a-f]{64}$")
        self.assertTrue(orca_pin.asset_amd64.endswith(".AppImage"))
        self.assertTrue(orca_pin.asset_arm64.endswith(".AppImage"))
        ci_version, ci_sha = bump.parse_orca_ci((ROOT / ".github" / "workflows" / "ci.yml").read_text())
        self.assertEqual(ci_version, orca_pin.version)
        self.assertRegex(ci_sha, r"^[0-9a-f]{64}$")


class Go2rtcPlanning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = fixture_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def api_for(self, release, commit="b" * 40):
        return FakeApi(
            releases={bump.GO2RTC_REPO: release},
            commits={(bump.GO2RTC_REPO, release.tag): commit},
        )

    def test_current_pin_skips(self):
        plan = bump.plan_go2rtc(self.root, self.api_for(make_release("1.9.15")), NOW)
        self.assertEqual(plan.action, "current")

    def test_young_release_waits(self):
        plan = bump.plan_go2rtc(self.root, self.api_for(make_release("1.9.16", age_days=1)), NOW)
        self.assertEqual(plan.action, "too-young")

    def test_bump_uses_asset_digests_and_tag_commit(self):
        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", "sha256:" + "1" * 64),
            asset("go2rtc_linux_arm64", "sha256:" + "2" * 64),
        ])
        plan = bump.plan_go2rtc(self.root, self.api_for(release, commit="c" * 40), NOW)
        self.assertEqual(plan.action, "bump")
        plan.apply(self.root)
        pin = bump.parse_go2rtc((self.root / "docker" / "go2rtc-prebuilt.sh").read_text())
        self.assertEqual(pin.version, "1.9.16")
        self.assertEqual(pin.revision, "c" * 7)  # same 7-char form as the file
        self.assertEqual(pin.sha256_amd64, "1" * 64)
        self.assertEqual(pin.sha256_arm64, "2" * 64)
        self.assertTrue(any("digest field" in line for line in plan.provenance))

    def test_bump_moves_every_recorded_surface(self):
        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", "sha256:" + "1" * 64),
            asset("go2rtc_linux_arm64", "sha256:" + "2" * 64),
        ])
        plan = bump.plan_go2rtc(self.root, self.api_for(release, commit="c" * 40), NOW)
        plan.apply(self.root)
        guard = (self.root / "tests" / "test_go2rtc_container.py").read_text()
        self.assertIn("version=1.9.16", guard)
        self.assertIn('AMD64_SHA = "' + "1" * 64 + '"', guard)
        self.assertIn('ARM64_SHA = "' + "2" * 64 + '"', guard)
        self.assertIn("`1.9.16`, revision `ccccccc`", (self.root / "docs" / "COMPONENTS.md").read_text())
        self.assertIn("go2rtc v1.9.16", (self.root / "docs" / "DOCKER.md").read_text())
        self.assertIn("(v1.9.16,", (self.root / "docs" / "SETTINGS.md").read_text())
        self.assertEqual(
            set(plan.files),
            {"docker/go2rtc-prebuilt.sh", "tests/test_go2rtc_container.py",
             "docs/COMPONENTS.md", "docs/DOCKER.md", "docs/SETTINGS.md"},
        )

    def test_drifted_recorded_surface_stops_the_bump(self):
        components = self.root / "docs" / "COMPONENTS.md"
        components.write_text(components.read_text().replace("`1.9.15`, revision `7c05066`", "`1.9.15`"))
        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", "sha256:" + "1" * 64),
            asset("go2rtc_linux_arm64", "sha256:" + "2" * 64),
        ])
        with self.assertRaises(bump.BumpError):
            bump.plan_go2rtc(self.root, self.api_for(release), NOW)

    def test_missing_digest_falls_back_to_download_and_hash(self):
        import hashlib

        release = make_release("1.9.16", assets=[
            asset("go2rtc_linux_amd64", None),
            asset("go2rtc_linux_arm64", ""),
        ])
        api = self.api_for(release)
        plan = bump.plan_go2rtc(self.root, api, NOW)
        expected = hashlib.sha256(b"binary").hexdigest()
        self.assertIn(f"sha256_amd64: {bump.parse_go2rtc(GO2RTC_TEXT).sha256_amd64} -> {expected}", plan.changes)
        self.assertTrue(any("hashed locally" in line for line in plan.provenance))

    def test_missing_asset_is_an_error(self):
        release = make_release("1.9.16", assets=[])
        with self.assertRaises(bump.BumpError):
            bump.plan_go2rtc(self.root, self.api_for(release), NOW)


class SpoolmanPlanning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = fixture_root(self.tmp.name)
        self.pin_ref = bump.parse_spoolman(DOCKERFILE_TEXT)

    def tearDown(self):
        self.tmp.cleanup()

    def api_for(self, release, commit, compare="ahead"):
        return FakeApi(
            releases={bump.SPOOLMAN_REPO: release},
            commits={(bump.SPOOLMAN_REPO, release.tag): commit},
            compare=compare,
        )

    def test_pin_at_latest_release_is_current(self):
        plan = bump.plan_spoolman(self.root, self.api_for(make_release("0.27.0"), self.pin_ref), NOW)
        self.assertEqual(plan.action, "current")

    def test_bump_to_newer_release(self):
        new_ref = "e" * 40
        plan = bump.plan_spoolman(self.root, self.api_for(make_release("0.27.1"), new_ref), NOW)
        self.assertEqual(plan.action, "bump")
        plan.apply(self.root)
        self.assertEqual(bump.parse_spoolman((self.root / "Dockerfile").read_text()), new_ref)
        # The recorded surfaces move with the pin.
        settings = (self.root / "docs" / "SETTINGS.md").read_text()
        self.assertIn(new_ref, settings)
        self.assertIn("Pinned Spoolman v0.27.1 source commit", settings)
        docker_doc = (self.root / "docs" / "DOCKER.md").read_text()
        self.assertIn(f"Spoolman 0.27.1 is built from exact upstream revision `{new_ref}`", docker_doc)
        self.assertEqual(set(plan.files), {"Dockerfile", "docs/SETTINGS.md", "docs/DOCKER.md"})

    def test_disagreeing_recorded_surfaces_stop_the_bump(self):
        settings = self.root / "docs" / "SETTINGS.md"
        settings.write_text(settings.read_text().replace("v0.27.0", "v0.26.9"))
        with self.assertRaises(bump.BumpError):
            bump.plan_spoolman(self.root, self.api_for(make_release("0.27.1"), "e" * 40), NOW)

    def test_pin_ahead_of_release_is_not_a_bump(self):
        plan = bump.plan_spoolman(
            self.root, self.api_for(make_release("0.27.1"), "e" * 40, compare="behind"), NOW
        )
        self.assertEqual(plan.action, "not-behind")

    def test_young_release_waits(self):
        plan = bump.plan_spoolman(
            self.root, self.api_for(make_release("0.27.1", age_days=2), "e" * 40), NOW
        )
        self.assertEqual(plan.action, "too-young")


def fake_fetcher(with_tree=True):
    """A fetch_bambu_source stand-in: records calls and builds a tree
    from the fixture Bambu Studio files (or an empty tree)."""
    import shutil

    calls = []

    def fetch(commit, dest):
        calls.append(commit)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        if with_tree:
            shutil.copytree(FIXTURES / "bambu_studio", dest, dirs_exist_ok=True)
        return dest

    return fetch, calls


class BambuPlanning(unittest.TestCase):
    PIN_DATE = datetime(2026, 8, 21, 4, 29, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = fixture_root(self.tmp.name)
        self.pin_ref = bump.parse_bambu(DOCKERFILE_TEXT)

    def tearDown(self):
        self.tmp.cleanup()

    def api_for(self, release, commit="d" * 40):
        return FakeApi(
            releases={bump.BAMBU_REPO: release},
            commits={(bump.BAMBU_REPO, release.tag): commit},
            commit_dates={(bump.BAMBU_REPO, self.pin_ref): self.PIN_DATE},
        )

    def release(self, **kwargs):
        kwargs.setdefault("tag", "v02.09.01.70")
        return make_release("02.09.01.70", **kwargs)

    def test_pin_at_latest_release_is_current(self):
        fetch, calls = fake_fetcher()
        plan = bump.plan_bambu(self.root, self.api_for(self.release(), commit=self.pin_ref), NOW, fetcher=fetch)
        self.assertEqual(plan.action, "current")
        self.assertEqual(calls, [])

    def test_release_not_after_pin_date_is_not_newer(self):
        # Published before the pinned commit was made: not a bump,
        # however old the release is.
        fetch, calls = fake_fetcher()
        plan = bump.plan_bambu(self.root, self.api_for(self.release(age_days=90)), NOW, fetcher=fetch)
        self.assertEqual(plan.action, "not-newer")
        self.assertEqual(calls, [])

    def test_young_release_waits(self):
        fetch, calls = fake_fetcher()
        plan = bump.plan_bambu(self.root, self.api_for(self.release(age_days=1)), NOW, fetcher=fetch)
        self.assertEqual(plan.action, "too-young")
        self.assertEqual(calls, [])

    def test_bump_moves_every_surface_and_regenerates_hms(self):
        fetch, calls = fake_fetcher()
        new_ref = "d" * 40
        plan = bump.plan_bambu(self.root, self.api_for(self.release(), commit=new_ref), NOW, fetcher=fetch)
        self.assertEqual(plan.action, "bump")
        self.assertEqual(calls, [new_ref])
        plan.apply(self.root)
        self.assertEqual(bump.parse_bambu((self.root / "Dockerfile").read_text()), new_ref)
        settings = (self.root / "docs" / "SETTINGS.md").read_text()
        self.assertIn(f"`{new_ref}`", settings)
        self.assertIn("Pinned Bambu Studio 02.09.01.70 source commit", settings)
        self.assertNotIn(self.pin_ref, settings)
        components = (self.root / "docs" / "COMPONENTS.md").read_text()
        self.assertIn(f"commit `{new_ref[:8]}…`", components)
        connectivity = (self.root / "docs" / "BAMBU-CONNECTIVITY.md").read_text()
        self.assertEqual(connectivity.count(new_ref), 2)
        self.assertNotIn("b94b6da251f8d5cf2773f2a78aa87c613ea9a907", connectivity)
        table = json.loads((self.root / "print_handy" / "printers" / "bambulab" / "hms_data.json").read_text())
        self.assertEqual(table["_provenance"]["bambu_studio_commit"], new_ref)
        # The regenerated table is the fixture studio files' content:
        # the tie between prefixes goes to the lowest prefix (093),
        # the differing 094 text stays a per-model override, and the
        # listed-but-empty text is preserved as empty.
        self.assertEqual(table["hms"]["0300010000010001"], "The extruder is clogged.")
        self.assertEqual(table["hms_by_model"]["094"]["0300010000010001"], "Extruder clogged with filament.")
        self.assertEqual(table["hms"]["0500010000010002"], "")
        self.assertEqual(table["error"]["05000001"], "Print failed due to an error.")
        self.assertEqual(
            set(plan.files),
            {"Dockerfile", "docs/SETTINGS.md", "docs/COMPONENTS.md",
             "docs/BAMBU-CONNECTIVITY.md", "print_handy/printers/bambulab/hms_data.json"},
        )
        self.assertTrue(any("--check" in line for line in plan.provenance))

    def test_drifted_settings_row_stops_the_bump_before_any_fetch(self):
        settings = self.root / "docs" / "SETTINGS.md"
        settings.write_text(settings.read_text().replace(self.pin_ref, "0" * 40))
        fetch, calls = fake_fetcher()
        with self.assertRaises(bump.BumpError):
            bump.plan_bambu(self.root, self.api_for(self.release()), NOW, fetcher=fetch)
        self.assertEqual(calls, [])

    def test_drifted_connectivity_record_stops_the_bump(self):
        connectivity = self.root / "docs" / "BAMBU-CONNECTIVITY.md"
        connectivity.write_text(connectivity.read_text().replace(
            "b94b6da251f8d5cf2773f2a78aa87c613ea9a907", "1" * 40, 1))
        fetch, calls = fake_fetcher()
        with self.assertRaises(bump.BumpError):
            bump.plan_bambu(self.root, self.api_for(self.release()), NOW, fetcher=fetch)
        self.assertEqual(calls, [])

    def test_moved_upstream_layout_stops_for_a_human(self):
        fetch, _ = fake_fetcher(with_tree=False)
        with self.assertRaises(bump.BumpError) as caught:
            bump.plan_bambu(self.root, self.api_for(self.release()), NOW, fetcher=fetch)
        self.assertIn("layout", str(caught.exception))
        # Nothing was written: the pin still stands.
        self.assertEqual(bump.parse_bambu((self.root / "Dockerfile").read_text()), self.pin_ref)


class HmsRegeneration(unittest.TestCase):
    def test_regenerate_is_deterministic_and_records_the_commit(self):
        hms_dir = FIXTURES / "bambu_studio" / "resources" / "hms"
        first = bump.regenerate_hms(hms_dir, "f" * 40)
        second = bump.regenerate_hms(hms_dir, "f" * 40)
        self.assertEqual(first, second)
        table = json.loads(first)
        self.assertEqual(table["_provenance"]["bambu_studio_commit"], "f" * 40)
        self.assertEqual(table["_provenance"]["prefixes"], ["093", "094"])

    def test_locate_hms_dir_rejects_a_tree_without_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bump.BumpError):
                bump.locate_hms_dir(Path(tmp))


class OrcaPinSurfaces(unittest.TestCase):
    """Against the frozen fixtures; the values below are the fixture's,
    frozen on purpose so future production bumps cannot break them."""

    def test_orca_parse_fixture(self):
        pin = bump.parse_orca(ORCA_TEXT)
        self.assertEqual(pin.version, "2.5.0-rc")
        self.assertEqual(pin.revision, "af2ffb8cddd2f3c0bdadc56d9cc3945fb23577ea")
        self.assertEqual(pin.sha256_amd64, "203104a3b0a880975b476ff00cfdd3a8fd2beb95fd8bfb2a9a28fb2bcbb47159")
        self.assertEqual(pin.sha256_arm64, "fdd29d685ed54671a1a2887684fff4d888bd98b74809154a9731a788a7c6dced")
        self.assertEqual(pin.asset_amd64, "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0-rc.AppImage")
        self.assertEqual(pin.asset_arm64, "OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0-rc.AppImage")

    def test_orca_round_trip(self):
        pin = bump.parse_orca(ORCA_TEXT)
        # Rendering the parsed values back changes nothing at all.
        self.assertEqual(bump.render_orca(ORCA_TEXT, pin), ORCA_TEXT)
        pin.version = "2.5.0"
        pin.revision = "e" * 40
        out = bump.render_orca(ORCA_TEXT, pin)
        again = bump.parse_orca(out)
        self.assertEqual(again.version, "2.5.0")
        self.assertEqual(again.revision, "e" * 40)
        # The narrative comment inside the block is not a pin record
        # and is not rewritten; the block's surroundings never move.
        self.assertIn("Print Handy ships ONE bundled OrcaSlicer: v2.5.0-rc", out)
        self.assertTrue(out.startswith(ORCA_TEXT.split("stable)")[0]))

    def test_orca_missing_block_is_an_error(self):
        with self.assertRaises(bump.BumpError):
            bump.parse_orca("#!/bin/sh\nversion=1.0\n")

    def test_orca_ci_parse_fixture(self):
        text = (FIXTURES / ".github" / "workflows" / "ci.yml").read_text()
        version, sha256 = bump.parse_orca_ci(text)
        self.assertEqual(version, "2.5.0-rc")
        self.assertEqual(sha256, "f0140100eb3d26d021b68347da9d5ce8536fe78d3e839d6abd83f40b27457175")


class OrcaTargetSelection(unittest.TestCase):
    def target(self, *releases):
        return bump.select_orca_target(list(releases)).version

    def test_rc_beats_beta(self):
        self.assertEqual(
            self.target(make_orca_release("2.5.0-beta", prerelease=True),
                        make_orca_release("2.5.0-rc", prerelease=True)),
            "2.5.0-rc",
        )

    def test_final_beats_its_rc(self):
        self.assertEqual(
            self.target(make_orca_release("2.5.0-rc", prerelease=True),
                        make_orca_release("2.5.0")),
            "2.5.0",
        )

    def test_newer_lines_prerelease_beats_older_final(self):
        self.assertEqual(
            self.target(make_orca_release("2.5.0"),
                        make_orca_release("2.6.0-beta", prerelease=True)),
            "2.6.0-beta",
        )

    def test_no_releases_is_an_error(self):
        with self.assertRaises(bump.BumpError):
            bump.select_orca_target([])


def fake_orca_fetcher(calls):
    """A fetch_orca_bundle stand-in: records calls and builds a fake
    extracted bundle (resources/profiles with all six vendors'
    packed caches, no JSON trees)."""

    def fetch(api, release, asset_name, sha256, dest):
        calls.append(asset_name)
        profiles = Path(dest) / "squashfs-root" / "resources" / "profiles"
        profiles.mkdir(parents=True)
        for vendor in bump.ORCA_VENDORS.values():
            (profiles / f"{vendor}.opc").write_bytes(b"packed")
        return profiles.parent

    return fetch


def matched_sweep():
    """A sweep result where every model matches in both packagings."""
    entry = {"version": "1.0", "sha256": {}, "matched": ["a recorded identity"]}
    return {
        pkg: {"model-a": {"tree": dict(entry), "packed": dict(entry)}}
        for pkg in bump.ORCA_VENDORS
    }


def fake_orca_source_fetcher(calls, with_tree=True):
    """A fetch_orca_source stand-in: builds a source tree holding
    the six vendors' JSON indexes (or an empty tree)."""

    def fetch(commit, dest):
        calls.append(commit)
        dest = Path(dest)
        profiles = dest / "resources" / "profiles"
        profiles.mkdir(parents=True, exist_ok=True)
        if with_tree:
            for vendor in bump.ORCA_VENDORS.values():
                (profiles / f"{vendor}.json").write_text("{}")
                (profiles / vendor).mkdir(exist_ok=True)
        return dest

    return fetch


class OrcaPlanning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = fixture_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def api_for(self, releases, commit="e" * 40):
        commits = {(bump.ORCA_REPO, release.tag): commit for release in releases}
        return FakeApi(release_lists={bump.ORCA_REPO: releases}, commits=commits)

    def plan(self, releases, fetcher=None, source_fetcher=None, sweeper=None):
        return bump.plan_orca(
            self.root, self.api_for(releases), NOW,
            fetcher=fetcher or fake_orca_fetcher([]),
            source_fetcher=source_fetcher or fake_orca_source_fetcher([]),
            sweeper=sweeper or (lambda root, tree, packed: matched_sweep()),
        )

    def test_current_pin_skips(self):
        calls = []
        plan = self.plan([make_orca_release("2.5.0-rc", prerelease=True, age_days=2)],
                         fetcher=fake_orca_fetcher(calls))
        self.assertEqual(plan.action, "current")
        self.assertEqual(calls, [])

    def test_young_final_waits(self):
        calls = []
        plan = self.plan([make_orca_release("2.5.0", age_days=1)],
                         fetcher=fake_orca_fetcher(calls))
        self.assertEqual(plan.action, "too-young")
        self.assertEqual(calls, [])

    def test_young_prerelease_proceeds(self):
        plan = self.plan([make_orca_release("2.6.0-beta", prerelease=True, age_days=0,
                                            assets=orca_assets("2.6.0-beta"))])
        self.assertEqual(plan.action, "bump")
        self.assertEqual(plan.version, "2.6.0-beta")

    def test_bump_moves_every_recorded_surface(self):
        calls = []
        plan = self.plan([make_orca_release("2.5.0", assets=orca_assets("2.5.0"))],
                         fetcher=fake_orca_fetcher(calls))
        self.assertEqual(plan.action, "bump")
        self.assertEqual(calls, ["OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage"])
        plan.apply(self.root)
        pin = bump.parse_orca((self.root / "docker" / "orca-prebuilt.sh").read_text())
        self.assertEqual(pin.version, "2.5.0")
        self.assertEqual(pin.revision, "e" * 40)
        self.assertEqual(pin.sha256_amd64, "3" * 64)
        self.assertEqual(pin.sha256_arm64, "4" * 64)
        self.assertEqual(pin.asset_amd64, "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage")
        ci = (self.root / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("ORCA_BUNDLE_VERSION: 2.5.0\n", ci)
        self.assertIn("ORCA_BUNDLE_SHA256: " + "5" * 64, ci)
        sha_test = (self.root / "tests" / "test_ci_bambu_workflow.py").read_text()
        self.assertIn('ORCA_BUNDLE_SHA256 = "' + "5" * 64 + '"', sha_test)
        guard = (self.root / "tests" / "test_docker_release.py").read_text()
        self.assertIn("version=2.5.0", guard)
        self.assertIn("revision=" + "e" * 40, guard)
        self.assertIn('"2.5.0"', guard)
        self.assertIn('"' + "e" * 40 + '"', guard)
        self.assertIn("OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage", guard)
        contract = (self.root / "tests" / "test_docker_release_contract.py").read_text()
        self.assertIn("e" * 40, contract)
        self.assertNotIn("af2ffb8c", contract)
        facts = (self.root / "print_handy" / "printers" / "bambulab" / "facts.py").read_text()
        self.assertIn('("v2.5.0", "' + "e" * 40 + '")', facts)
        p1s = (self.root / "tests" / "printers" / "test_bambulab_p1s.py").read_text()
        self.assertIn('("v2.5.0", "' + "e" * 40 + '")', p1s)
        components = (self.root / "docs" / "COMPONENTS.md").read_text()
        self.assertIn("`2.5.0`, revision `eeeeeeee…`", components)
        self.assertIn("now v2.5.0)", (self.root / "docs" / "DOCKER.md").read_text())
        settings = (self.root / "docs" / "SETTINGS.md").read_text()
        self.assertIn("single build (v2.5.0)", settings)
        self.assertIn("release (v2.5.0, SHA256 verified", settings)
        self.assertIn("bundled OrcaSlicer (v2.5.0):", (self.root / ".env.example").read_text())
        self.assertIn("one bundled build (v2.5.0)", (self.root / "README.md").read_text())
        unraid = (self.root / "docker" / "unraid" / "print-handy.xml").read_text()
        self.assertIn("bundled OrcaSlicer (v2.5.0) and stable", unraid)
        for name in ("ANYCUBIC", "CREALITY", "ELEGOO", "PRUSA", "QIDI"):
            self.assertIn("OrcaSlicer v2.5.0 installation",
                          (self.root / "docs" / f"{name}.md").read_text())
        a1 = (self.root / "docs" / "BAMBU-A1-DOCKER.md").read_text()
        self.assertIn("bundled OrcaSlicer v2.5.0", a1)
        self.assertIn("pinned OrcaSlicer v2.5.0 AppImage", a1)
        self.assertEqual(
            set(plan.files),
            {"docker/orca-prebuilt.sh", ".github/workflows/ci.yml",
             "tests/test_ci_bambu_workflow.py", "tests/test_docker_release.py",
             "tests/test_docker_release_contract.py",
             "print_handy/printers/bambulab/facts.py",
             "tests/printers/test_bambulab_p1s.py",
             "docs/COMPONENTS.md", "docs/DOCKER.md", "docs/SETTINGS.md",
             ".env.example", "README.md", "docker/unraid/print-handy.xml",
             "docs/ANYCUBIC.md", "docs/CREALITY.md", "docs/ELEGOO.md",
             "docs/PRUSA.md", "docs/QIDI.md", "docs/BAMBU-A1-DOCKER.md"},
        )
        self.assertTrue(any("digest field" in line for line in plan.provenance))
        self.assertTrue(any("identity sweep: all 6 identity-checked models" in line
                            for line in plan.provenance))

    def test_drifted_recorded_surface_stops_the_bump_before_any_download(self):
        components = self.root / "docs" / "COMPONENTS.md"
        components.write_text(components.read_text().replace("`2.5.0-rc`, revision `af2ffb8c…`", "`2.5.0-rc`"))
        calls = []
        with self.assertRaises(bump.BumpError):
            self.plan([make_orca_release("2.5.0", assets=orca_assets("2.5.0"))],
                      fetcher=fake_orca_fetcher(calls))
        self.assertEqual(calls, [])

    def test_ci_pin_disagreement_stops_the_bump(self):
        ci = self.root / ".github" / "workflows" / "ci.yml"
        ci.write_text(ci.read_text().replace("ORCA_BUNDLE_VERSION: 2.5.0-rc",
                                             "ORCA_BUNDLE_VERSION: 2.5.0-beta"))
        calls = []
        with self.assertRaises(bump.BumpError):
            self.plan([make_orca_release("2.5.0", assets=orca_assets("2.5.0"))],
                      fetcher=fake_orca_fetcher(calls))
        self.assertEqual(calls, [])

    def test_unmatched_identity_stops_and_writes_nothing(self):
        def sweeper(root, tree, packed):
            results = matched_sweep()
            results["creality"]["model-a"]["tree"] = {
                "version": "2.0", "sha256": {}, "matched": [],
            }
            return results

        with self.assertRaises(bump.BumpError) as caught:
            self.plan([make_orca_release("2.5.0", assets=orca_assets("2.5.0"))],
                      sweeper=sweeper)
        self.assertIn("creality/model-a", str(caught.exception))
        pin = bump.parse_orca((self.root / "docker" / "orca-prebuilt.sh").read_text())
        self.assertEqual(pin.version, "2.5.0-rc")

    def test_missing_asset_is_an_error(self):
        assets = [a for a in orca_assets("2.5.0") if "aarch64" not in a["name"]]
        with self.assertRaises(bump.BumpError):
            self.plan([make_orca_release("2.5.0", assets=assets)])

    def test_missing_digest_falls_back_to_download_and_hash(self):
        import hashlib

        assets = orca_assets("2.5.0")
        assets[0] = asset(assets[0]["name"], None)
        plan = self.plan([make_orca_release("2.5.0", assets=assets)])
        expected = hashlib.sha256(b"binary").hexdigest()
        self.assertIn(expected, plan.changes[2])
        self.assertTrue(any("hashed locally" in line for line in plan.provenance))

    def test_moved_source_layout_stops_for_a_human(self):
        source_calls = []
        with self.assertRaises(bump.BumpError) as caught:
            self.plan([make_orca_release("2.5.0", assets=orca_assets("2.5.0"))],
                      source_fetcher=fake_orca_source_fetcher(source_calls, with_tree=False))
        self.assertIn("layout", str(caught.exception))
        # Nothing was written: the pin still stands.
        pin = bump.parse_orca((self.root / "docker" / "orca-prebuilt.sh").read_text())
        self.assertEqual(pin.version, "2.5.0-rc")


class OrcaVerifyIdentities(unittest.TestCase):
    def test_all_matched_summarizes(self):
        lines = bump.verify_orca_identities(matched_sweep())
        self.assertEqual(len(lines), len(bump.ORCA_VENDORS) + 1)
        self.assertIn("all 6 identity-checked models", lines[-1])

    def test_unmatched_model_is_named(self):
        results = matched_sweep()
        results["qidi"]["model-a"]["packed"]["matched"] = []
        with self.assertRaises(bump.BumpError) as caught:
            bump.verify_orca_identities(results)
        self.assertIn("qidi/model-a (packed)", str(caught.exception))

    def test_flatten_error_is_named(self):
        results = matched_sweep()
        results["prusa"]["model-a"]["tree"] = {"error": "ProfileError: boom"}
        with self.assertRaises(bump.BumpError) as caught:
            bump.verify_orca_identities(results)
        self.assertIn("prusa/model-a (tree): ProfileError: boom", str(caught.exception))


class OrcaBundleHandling(unittest.TestCase):
    def test_stage_packed_profiles_copies_only_the_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profiles = base / "resources" / "profiles"
            profiles.mkdir(parents=True)
            for vendor in bump.ORCA_VENDORS.values():
                (profiles / f"{vendor}.opc").write_bytes(b"packed")
                (profiles / vendor).mkdir()
            staged = bump.stage_packed_profiles(base / "resources", base / "packed")
            names = sorted(p.name for p in (staged / "profiles").iterdir())
            self.assertEqual(names, sorted(f"{v}.opc" for v in bump.ORCA_VENDORS.values()))

    def test_stage_packed_profiles_missing_cache_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "resources" / "profiles").mkdir(parents=True)
            with self.assertRaises(bump.BumpError) as caught:
                bump.stage_packed_profiles(base / "resources", base / "packed")
            self.assertIn("packed preset cache", str(caught.exception))

    def test_fetch_verifies_and_extracts_a_fake_appimage(self):
        import hashlib

        script = b"#!/bin/sh\nmkdir -p squashfs-root/resources/profiles\n"
        name = "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage"
        release = make_orca_release("2.5.0", assets=[asset(name, None)])
        api = FakeApi(download_data=script)
        with tempfile.TemporaryDirectory() as tmp:
            resources = bump.fetch_orca_bundle(
                api, release, name, hashlib.sha256(script).hexdigest(), Path(tmp) / "bundle"
            )
            self.assertTrue((resources / "profiles").is_dir())

    def test_fetch_rejects_a_wrong_hash(self):
        name = "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0.AppImage"
        release = make_orca_release("2.5.0", assets=[asset(name, None)])
        api = FakeApi(download_data=b"#!/bin/sh\ntrue\n")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(bump.BumpError):
                bump.fetch_orca_bundle(api, release, name, "0" * 64, Path(tmp) / "bundle")

    def test_locate_orca_tree_accepts_and_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fetch = fake_orca_source_fetcher([])
            tree = fetch("e" * 40, base / "src")
            self.assertEqual(bump.locate_orca_tree(tree), base / "src" / "resources")
            empty = fake_orca_source_fetcher([], with_tree=False)("e" * 40, base / "empty")
            with self.assertRaises(bump.BumpError):
                bump.locate_orca_tree(empty)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class OrcaSweepDriver(unittest.TestCase):
    """The real sweep driver against a synthetic package: a stub
    print_handy whose bambulab profiles pipeline is the production
    module copied in, one fake Creality model, and a minimal vendor
    tree built by hand. Pass 1 records no identity, so nothing
    matches; the test then records pass 1's own hashes as the
    model's identity and pass 2 must match them - the plumbing
    (enumeration, variant handling, matching) is what is proven
    here; the non-circular proof against real data is the live
    re-derivation of the shipped bundle's identities."""

    FACTS_TEMPLATE = '''
class _Fact:
    def __init__(self, value):
        self.value = value


MODELS = {"mini": {
    "orca_profiles": _Fact({"machine": "Mini Machine", "process": "Mini Process",
                            "filament": "Mini Filament"}),
    "orca_identity": _Fact(%s),
}}
'''

    def build(self, base):
        import shutil

        root = Path(base) / "repo"
        pkg = root / "print_handy" / "printers"
        _write(root / "print_handy" / "__init__.py", "")
        _write(pkg / "__init__.py", "")
        _write(pkg / "bambulab" / "__init__.py", "")
        _write(pkg / "creality" / "__init__.py", "")
        _write(pkg / "bambulab" / "facts.py", "")
        for name in ("profiles.py", "preset_cache.py"):
            shutil.copyfile(ROOT / "print_handy" / "printers" / "bambulab" / name,
                            pkg / "bambulab" / name)
        profiles = Path(base) / "resources" / "profiles"
        index = {
            "version": "9.9.9",
            "machine_list": [{"name": "Mini Machine", "sub_path": "mini_machine.json"}],
            "process_list": [{"name": "Mini Process", "sub_path": "mini_process.json"}],
            "filament_list": [{"name": "Mini Filament", "sub_path": "mini_filament.json"}],
        }
        _write(profiles / "Creality.json", json.dumps(index))
        _write(profiles / "Creality" / "mini_machine.json",
               json.dumps({"type": "machine", "name": "Mini Machine", "printer_model": "Mini"}))
        _write(profiles / "Creality" / "mini_process.json",
               json.dumps({"type": "process", "name": "Mini Process", "layer_height": "0.2"}))
        _write(profiles / "Creality" / "mini_filament.json",
               json.dumps({"type": "filament", "name": "Mini Filament", "filament_type": ["PLA"]}))
        packed = Path(base) / "packed"
        (packed / "profiles").mkdir(parents=True)
        return root, Path(base) / "resources", packed

    def facts_path(self, root):
        return root / "print_handy" / "printers" / "creality" / "facts.py"

    def test_driver_enumerates_flattens_and_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, tree, packed = self.build(tmp)
            _write(self.facts_path(root), self.FACTS_TEMPLATE % "[]")
            first = bump.sweep_orca_identities(root, tree, packed, families=["creality"])
            entry = first["creality"]["mini"]["tree"]
            self.assertEqual(entry["version"], "9.9.9")
            self.assertEqual(entry["matched"], [])
            self.assertEqual(sorted(entry["sha256"]), ["filament", "machine", "process"])
            # The packed variant holds neither tree nor cache here.
            self.assertIn("error", first["creality"]["mini"]["packed"])
            recorded = [{"label": "fixture identity", "vendor_version": "9.9.9",
                         "sha256": entry["sha256"]}]
            _write(self.facts_path(root), self.FACTS_TEMPLATE % repr(recorded))
            second = bump.sweep_orca_identities(root, tree, packed, families=["creality"])
            self.assertEqual(second["creality"]["mini"]["tree"]["matched"], ["fixture identity"])

    BBL_FACTS_TEMPLATE = '''
class _Fact:
    def __init__(self, value):
        self.value = value


MODELS = {"mini": {
    "orca_profiles": _Fact({"machine": "Mini Machine", "process": "Mini Process",
                            "filament": "Mini Filament"}),
    "orca_filaments": _Fact({"PLA": "Mini Filament", "PETG": "Mini Filament PETG"}),
    "orca_identity": _Fact(%s),
}}
'''

    def build_bbl(self, base, root, tree):
        """Add a BBL mini tree (with a second, PETG filament profile)
        to the synthetic package and resources from build()."""
        profiles = tree / "profiles"
        index = {
            "version": "9.9.9",
            "machine_list": [{"name": "Mini Machine", "sub_path": "mini_machine.json"}],
            "process_list": [{"name": "Mini Process", "sub_path": "mini_process.json"}],
            "filament_list": [{"name": "Mini Filament", "sub_path": "mini_filament.json"},
                              {"name": "Mini Filament PETG", "sub_path": "mini_filament_petg.json"}],
        }
        _write(profiles / "BBL.json", json.dumps(index))
        _write(profiles / "BBL" / "mini_machine.json",
               json.dumps({"type": "machine", "name": "Mini Machine", "printer_model": "Mini"}))
        _write(profiles / "BBL" / "mini_process.json",
               json.dumps({"type": "process", "name": "Mini Process", "layer_height": "0.2"}))
        _write(profiles / "BBL" / "mini_filament.json",
               json.dumps({"type": "filament", "name": "Mini Filament", "filament_type": ["PLA"]}))
        _write(profiles / "BBL" / "mini_filament_petg.json",
               json.dumps({"type": "filament", "name": "Mini Filament PETG",
                           "filament_type": ["PETG"]}))
        return root / "print_handy" / "printers" / "bambulab" / "facts.py"

    def test_driver_verifies_declared_filament_profiles(self):
        # A model whose orca_filaments declares a non-default profile:
        # the identity matches only when that profile also flattens
        # to the identity's recorded filament hash - a changed,
        # misrecorded or removed profile must not match.
        sys.path.insert(0, str(ROOT))
        from print_handy.printers.bambulab import profiles as production_profiles

        with tempfile.TemporaryDirectory() as tmp:
            root, tree, packed = self.build(tmp)
            facts = self.build_bbl(tmp, root, tree)
            _write(facts, self.BBL_FACTS_TEMPLATE % "[]")
            first = bump.sweep_orca_identities(root, tree, packed, families=["bambulab"])
            entry = first["bambulab"]["mini"]["tree"]
            self.assertEqual(entry["matched"], [])
            flat = production_profiles.flatten_filaments(tree, ["Mini Filament PETG"])
            petg_hash = production_profiles.sha256(flat["Mini Filament PETG"])
            recorded = [{"label": "fixture identity", "vendor_version": "9.9.9",
                         "sha256": entry["sha256"],
                         "filaments": {"Mini Filament PETG": petg_hash}}]
            _write(facts, self.BBL_FACTS_TEMPLATE % repr(recorded))
            second = bump.sweep_orca_identities(root, tree, packed, families=["bambulab"])
            self.assertEqual(second["bambulab"]["mini"]["tree"]["matched"],
                             ["fixture identity"])
            # A misrecorded filament hash breaks the match even though
            # the machine/process/default-filament triple still agrees.
            recorded[0]["filaments"]["Mini Filament PETG"] = "0" * 64
            _write(facts, self.BBL_FACTS_TEMPLATE % repr(recorded))
            third = bump.sweep_orca_identities(root, tree, packed, families=["bambulab"])
            self.assertEqual(third["bambulab"]["mini"]["tree"]["matched"], [])
            # A removed profile is an error entry, not a match.
            index = json.loads((tree / "profiles" / "BBL.json").read_text())
            index["filament_list"] = [e for e in index["filament_list"]
                                      if e["name"] != "Mini Filament PETG"]
            _write(tree / "profiles" / "BBL.json", json.dumps(index))
            fourth = bump.sweep_orca_identities(root, tree, packed, families=["bambulab"])
            self.assertIn("error", fourth["bambulab"]["mini"]["tree"])


class DedupeSupersede(unittest.TestCase):
    def test_same_version_open_pr_dedupes(self):
        prs = [pr_dict(7, "upstream/go2rtc-1.9.16")]
        decision, found = bump.decide_existing(prs, "go2rtc", "1.9.16")
        self.assertEqual(decision, "dedupe")
        self.assertEqual(found["number"], 7)

    def test_older_open_pr_is_superseded(self):
        prs = [pr_dict(7, "upstream/go2rtc-1.9.16")]
        decision, found = bump.decide_existing(prs, "go2rtc", "1.9.17")
        self.assertEqual(decision, "supersede")
        self.assertEqual([p["number"] for p in found], [7])

    def test_bambu_branches_dedupe_and_supersede(self):
        prs = [pr_dict(11, "upstream/bambu-02.09.01.70")]
        decision, found = bump.decide_existing(prs, "bambu", "02.09.01.70")
        self.assertEqual(decision, "dedupe")
        self.assertEqual(found["number"], 11)
        decision, found = bump.decide_existing(prs, "bambu", "02.10.00.01")
        self.assertEqual(decision, "supersede")
        self.assertEqual([p["number"] for p in found], [11])

    def test_orca_branches_dedupe_and_supersede(self):
        prs = [pr_dict(13, "upstream/orca-2.5.0-rc")]
        decision, found = bump.decide_existing(prs, "orca", "2.5.0-rc")
        self.assertEqual(decision, "dedupe")
        self.assertEqual(found["number"], 13)
        # The final supersedes its own open RC bump PR (prerelease-aware
        # ordering drives the supersede decision too).
        decision, found = bump.decide_existing(prs, "orca", "2.5.0")
        self.assertEqual(decision, "supersede")
        self.assertEqual([p["number"] for p in found], [13])

    def test_unlabeled_or_foreign_prs_are_never_touched(self):
        prs = [
            pr_dict(7, "upstream/go2rtc-1.9.16", labels=()),
            pr_dict(8, "upstream/spoolman-0.27.1"),
            pr_dict(9, "feat/something-else"),
        ]
        decision, _ = bump.decide_existing(prs, "go2rtc", "1.9.17")
        self.assertEqual(decision, "fresh")

    def test_execute_supersede_closes_old_with_pointer(self):
        release = make_release("0.27.1")
        plan = bump.Plan("spoolman", "bump", version="0.27.1", files=["Dockerfile"],
                         release=release)
        api = FakeApi(prs=[pr_dict(7, "upstream/spoolman-0.27.0")])
        git = FakeGit()
        outcome = bump.execute_plan(Path("."), api, "catduckgnaf/print-handy", plan, False, git=git, log=lambda m: None)
        self.assertEqual(outcome, "opened")
        self.assertEqual(api.closed, [7])
        self.assertIn("4242", api.comments[0][1])
        self.assertEqual(api.labeled, [(4242, ["dependencies", "upstream-bump"])])

    def test_execute_dedupe_noops(self):
        plan = bump.Plan("go2rtc", "bump", version="1.9.16", files=["docker/go2rtc-prebuilt.sh"],
                         release=make_release("1.9.16"))
        api = FakeApi(prs=[pr_dict(9, "upstream/go2rtc-1.9.16")])
        outcome = bump.execute_plan(Path("."), api, "catduckgnaf/print-handy", plan, False, git=FakeGit(), log=lambda m: None)
        self.assertEqual(outcome, "dedupe")
        self.assertEqual(api.created, [])


class IdentityGate(unittest.TestCase):
    def test_both_secrets_required(self):
        self.assertFalse(bump.identity_configured({}))
        self.assertFalse(bump.identity_configured({"UPSTREAM_BOT_APP_ID": "1"}))
        self.assertFalse(bump.identity_configured({"UPSTREAM_BOT_PRIVATE_KEY": "k"}))
        self.assertTrue(bump.identity_configured(
            {"UPSTREAM_BOT_APP_ID": "1", "UPSTREAM_BOT_PRIVATE_KEY": "k"}))

    def test_check_identity_output(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(bump.check_identity({}, out, err), 0)
        self.assertEqual(out.getvalue().strip(), "configured=false")
        self.assertIn("not configured", err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        bump.check_identity({"UPSTREAM_BOT_APP_ID": "1", "UPSTREAM_BOT_PRIVATE_KEY": VALID_KEY}, out, err)
        self.assertEqual(out.getvalue().strip(), "configured=true")

    def test_write_mode_without_token_is_inert(self):
        err = io.StringIO()
        rc = bump.main([], env={}, out=io.StringIO(), err=err)
        self.assertEqual(rc, 0)
        self.assertIn("inert", err.getvalue())

    def test_mark_ready_without_token_is_inert(self):
        err = io.StringIO()
        rc = bump.main(["--mark-ready", "--branch", "upstream/go2rtc-1.9.16"], env={}, out=io.StringIO(), err=err)
        self.assertEqual(rc, 0)
        self.assertIn("inert", err.getvalue())


class IdentityShapeValidation(unittest.TestCase):
    def test_numeric_app_id_ok(self):
        self.assertTrue(bump.valid_app_identifier("123456"))
        self.assertTrue(bump.valid_app_identifier("1"))

    def test_client_ids_ok(self):
        self.assertTrue(bump.valid_app_identifier("Iv1.abcdefgh"))
        self.assertTrue(bump.valid_app_identifier("Iv23liabdefgh123"))

    def test_slug_garbage_and_empty_rejected(self):
        self.assertFalse(bump.valid_app_identifier("print-handy-upstream-bot"))
        self.assertFalse(bump.valid_app_identifier("not an id"))
        self.assertFalse(bump.valid_app_identifier(""))
        self.assertFalse(bump.valid_app_identifier(None))
        self.assertFalse(bump.valid_app_identifier("Iv"))
        self.assertFalse(bump.valid_app_identifier("Iv1.x"))

    def test_pem_with_begin_and_end_ok(self):
        self.assertTrue(bump.valid_private_key(VALID_KEY))
        self.assertTrue(bump.valid_private_key(
            "-----BEGIN PRIVATE KEY-----\nZm9v\n-----END PRIVATE KEY-----\n"))

    def test_truncated_and_garbage_keys_rejected(self):
        self.assertFalse(bump.valid_private_key("k"))
        self.assertFalse(bump.valid_private_key(""))
        self.assertFalse(bump.valid_private_key(
            "-----BEGIN RSA PRIVATE KEY-----\nZm9v\n"))  # no footer
        self.assertFalse(bump.valid_private_key(
            "-----BEGIN RSA PRIVATE KEY-----\nZm9v\n"
            "-----END EC PRIVATE KEY-----\n"))  # mismatched footer

    def test_check_identity_malformed_key_returns_2(self):
        bad_key = "not-a-pem-secret-value"
        out, err = io.StringIO(), io.StringIO()
        rc = bump.check_identity(
            {"UPSTREAM_BOT_APP_ID": "123456", "UPSTREAM_BOT_PRIVATE_KEY": bad_key},
            out, err)
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue().strip(), "configured=false")
        self.assertIn("UPSTREAM_BOT_PRIVATE_KEY", err.getvalue())
        self.assertIn("malformed", err.getvalue())
        # The secret material is never echoed.
        self.assertNotIn(bad_key, out.getvalue() + err.getvalue())

    def test_check_identity_malformed_id_returns_2(self):
        out, err = io.StringIO(), io.StringIO()
        rc = bump.check_identity(
            {"UPSTREAM_BOT_APP_ID": "some-slug-value", "UPSTREAM_BOT_PRIVATE_KEY": VALID_KEY},
            out, err)
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue().strip(), "configured=false")
        self.assertIn("UPSTREAM_BOT_APP_ID", err.getvalue())
        self.assertNotIn("some-slug-value", err.getvalue())

    def test_check_identity_client_id_and_pem_ok(self):
        out, err = io.StringIO(), io.StringIO()
        rc = bump.check_identity(
            {"UPSTREAM_BOT_APP_ID": "Iv1.abcdefgh", "UPSTREAM_BOT_PRIVATE_KEY": VALID_KEY},
            out, err)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "configured=true")


class DiagnoseIdentity(unittest.TestCase):
    def opener_for(self, app, installation):
        """app/installation: (status, body) served by the fake opener."""
        import urllib.error

        def serve(status, body, url):
            if status != 200:
                raise urllib.error.HTTPError(url, status, "error", {}, None)

            class Resp:
                def read(self):
                    return json.dumps(body).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return Resp()

        def opener(req):
            if req.full_url.endswith("/installation"):
                return serve(installation[0], installation[1], req.full_url)
            if req.full_url.endswith("/app"):
                return serve(app[0], app[1], req.full_url)
            raise AssertionError(f"unexpected URL {req.full_url}")

        return opener

    def signer(self, captured):
        def sign(signing_input, private_key):
            captured.append(signing_input)
            return b"fake-signature-bytes"

        return sign

    def app_body(self, slug="print-handy-upstream-bot"):
        return {"id": 123456, "slug": slug, "name": "Print Handy Upstream Bot",
                "owner": {"login": "catduckgnaf"}}

    def run_diagnose(self, app, installation, env=None, now=None):
        out, err = io.StringIO(), io.StringIO()
        captured = []
        rc = bump.diagnose_identity(
            env or BOT_ENV, "catduckgnaf/print-handy", out, err,
            opener=self.opener_for(app, installation),
            signer=self.signer(captured), now=now)
        return rc, out.getvalue() + err.getvalue(), captured

    def test_installed_app_returns_0(self):
        rc, text, _ = self.run_diagnose(
            (200, self.app_body()),
            (200, {"id": 987, "repository_selection": "selected"}))
        self.assertEqual(rc, 0)
        self.assertIn("print-handy-upstream-bot", text)
        self.assertIn("987", text)
        self.assertIn("selected", text)

    def test_installation_404_names_both_causes_with_real_slug(self):
        rc, text, _ = self.run_diagnose((200, self.app_body()), (404, {}))
        self.assertEqual(rc, 3)
        self.assertIn("print-handy-upstream-bot", text)
        self.assertIn("Installations", text)
        self.assertIn("Repository access", text)
        self.assertIn("secrets belong to the wrong app", text)

    def test_app_401_gives_wrong_key_guidance(self):
        rc, text, _ = self.run_diagnose((401, {}), (200, {}))
        self.assertNotEqual(rc, 0)
        self.assertIn("private key does not belong to the identifier", text)

    def test_slug_mismatch_is_called_out(self):
        rc, text, _ = self.run_diagnose(
            (200, self.app_body(slug="totally-other-app")),
            (200, {"id": 987, "repository_selection": "all"}))
        self.assertEqual(rc, 0)
        self.assertIn("totally-other-app", text)
        self.assertIn("differently-named app", text)

    def test_jwt_claims_shape(self):
        import base64

        epoch = 1_700_000_000
        rc, _, captured = self.run_diagnose(
            (200, self.app_body()),
            (200, {"id": 987, "repository_selection": "selected"}),
            now=epoch)
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        header_b64, claims_b64 = captured[0].decode().split(".")

        def decode(segment):
            return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))

        self.assertEqual(decode(header_b64), {"alg": "RS256", "typ": "JWT"})
        claims = decode(claims_b64)
        self.assertEqual(claims["iss"], "123456")
        self.assertEqual(claims["iat"], epoch - 60)
        self.assertEqual(claims["exp"], epoch + 600)


class ReadyDecision(unittest.TestCase):
    def checks(self, *specs):
        return [
            {"name": name, "status": status, "conclusion": conclusion, "started_at": "2026-11-05T00:00:00Z"}
            for name, status, conclusion in specs
        ]

    def test_all_guards_pass(self):
        ok, _ = bump.ready_decision(
            pr_dict(3, "upstream/go2rtc-1.9.16"),
            self.checks(("CI", "completed", "success"), ("docker", "completed", "success")),
        )
        self.assertTrue(ok)

    def test_any_failure_blocks(self):
        ok, reason = bump.ready_decision(
            pr_dict(3, "upstream/go2rtc-1.9.16"),
            self.checks(("CI", "completed", "failure")),
        )
        self.assertFalse(ok)
        self.assertIn("CI", reason)

    def test_running_check_blocks(self):
        ok, _ = bump.ready_decision(
            pr_dict(3, "upstream/go2rtc-1.9.16"),
            self.checks(("CI", "in_progress", None)),
        )
        self.assertFalse(ok)

    def test_label_and_author_and_draft_required(self):
        good = self.checks(("CI", "completed", "success"))
        self.assertFalse(bump.ready_decision(pr_dict(3, "b", labels=()), good)[0])
        self.assertFalse(bump.ready_decision(pr_dict(3, "b", author="catduck"), good)[0])
        self.assertFalse(bump.ready_decision(pr_dict(3, "b", draft=False), good)[0])
        self.assertFalse(bump.ready_decision(None, good)[0])
        self.assertFalse(bump.ready_decision(pr_dict(3, "b"), [])[0])

    def test_latest_run_per_name_wins(self):
        runs = self.checks(("CI", "completed", "failure")) + [
            {"name": "CI", "status": "completed", "conclusion": "success", "started_at": "2026-11-05T01:00:00Z"}
        ]
        self.assertTrue(bump.ready_decision(pr_dict(3, "b"), runs)[0])

    def test_mark_ready_flow_marks_only_when_guards_pass(self):
        pr = pr_dict(3, "upstream/go2rtc-1.9.16")
        api = FakeApi(prs=[pr])
        api.checks[pr["head"]["sha"]] = self.checks(("CI", "completed", "success"))
        bump.mark_ready(Path("."), api, "catduckgnaf/print-handy", "upstream/go2rtc-1.9.16", log=lambda m: None)
        self.assertEqual(api.marked_ready, ["PR_node_3"])


class TransportLevelApi(unittest.TestCase):
    """GitHubApi against a canned opener: release picking and tag deref."""

    def canned(self, mapping):
        def opener(req):
            payload = mapping[req.full_url]
            body = json.dumps(payload).encode()

            class Resp:
                def read(self):
                    return body

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return Resp()

        return opener

    def test_latest_release_skips_prereleases_and_drafts(self):
        url = "https://api.github.com/repos/X/y/releases?per_page=30"
        api = bump.GitHubApi(opener=self.canned({url: [
            {"tag_name": "v2.0.0-rc", "name": "rc", "draft": False, "prerelease": True,
             "published_at": "2026-11-04T00:00:00Z", "html_url": "u1", "assets": []},
            {"tag_name": "v1.5.0", "name": "draft", "draft": True, "prerelease": False,
             "published_at": "2026-11-03T00:00:00Z", "html_url": "u2", "assets": []},
            {"tag_name": "v1.4.0", "name": "final", "draft": False, "prerelease": False,
             "published_at": "2026-11-01T00:00:00Z", "html_url": "u3", "assets": []},
        ]}))
        release = api.get_latest_release("X/y")
        self.assertEqual(release.version, "1.4.0")

    def test_list_releases_includes_prereleases_and_skips_drafts(self):
        url = "https://api.github.com/repos/X/y/releases?per_page=100"
        api = bump.GitHubApi(opener=self.canned({url: [
            {"tag_name": "v2.5.0-rc", "name": "rc", "draft": False, "prerelease": True,
             "published_at": "2026-11-01T00:00:00Z", "html_url": "u1", "assets": []},
            {"tag_name": "v2.5.0-beta", "name": "draft", "draft": True, "prerelease": True,
             "published_at": "2026-10-20T00:00:00Z", "html_url": "u2", "assets": []},
            {"tag_name": "v2.4.2", "name": "final", "draft": False, "prerelease": False,
             "published_at": "2026-09-01T00:00:00Z", "html_url": "u3", "assets": []},
        ]}))
        releases = api.list_releases("X/y")
        self.assertEqual([(r.version, r.prerelease) for r in releases],
                         [("2.5.0-rc", True), ("2.4.2", False)])

    def test_annotated_tag_dereferences_to_commit(self):
        base = "https://api.github.com/repos/X/y"
        api = bump.GitHubApi(opener=self.canned({
            f"{base}/git/ref/tags/v1.0.0": {"object": {"type": "tag", "sha": "t" * 40}},
            f"{base}/git/tags/{'t' * 40}": {"object": {"type": "commit", "sha": "c" * 40}},
        }))
        self.assertEqual(api.resolve_tag_commit("X/y", "v1.0.0"), "c" * 40)


class WorkflowShapes(unittest.TestCase):
    def load(self, path):
        return yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def test_bump_workflow_schedule_dispatch_and_identity(self):
        wf = self.load(BUMP_WORKFLOW)
        self.assertEqual(wf["permissions"]["contents"], "read")
        triggers = wf.get(True) or wf.get("on")  # 'on' key, however the loader spells it
        self.assertEqual(triggers["schedule"][0]["cron"], "7 15 * * 1")
        options = triggers["workflow_dispatch"]["inputs"]["component"]["options"]
        self.assertEqual(options, ["all", "go2rtc", "spoolman", "bambu", "orca"])
        self.assertEqual(wf["jobs"]["bump"]["timeout-minutes"], "45")
        steps = wf["jobs"]["bump"]["steps"]
        text = BUMP_WORKFLOW.read_text()
        self.assertIn("actions/create-github-app-token@v3", text)
        self.assertIn("UPSTREAM_BOT_APP_ID", text)
        self.assertIn("UPSTREAM_BOT_PRIVATE_KEY", text)
        token_step = next(s for s in steps if s.get("id") == "app-token")
        self.assertIn("configured", token_step["if"])
        # The token step uses the non-deprecated client-id input, and
        # the deprecated app-id input remains nowhere in either
        # workflow (#950).
        self.assertIn("UPSTREAM_BOT_APP_ID", token_step["with"]["client-id"])
        self.assertNotIn("app-id:", text)
        self.assertNotIn("app-id:", READY_WORKFLOW.read_text())
        ready_steps = self.load(READY_WORKFLOW)["jobs"]["mark-ready"]["steps"]
        ready_token = next(s for s in ready_steps if s.get("id") == "app-token")
        self.assertIn("UPSTREAM_BOT_APP_ID", ready_token["with"]["client-id"])
        # The diagnose step exists, is gated on the identity check,
        # and runs before the token mint so a broken identity fails
        # with the diagnosis instead of the mint's bare 404.
        diagnose_step = next(
            s for s in steps if "--diagnose-identity" in s.get("run", ""))
        self.assertIn("configured", diagnose_step["if"])
        self.assertLess(steps.index(diagnose_step), steps.index(token_step))
        run_step = next(s for s in steps if "bump_upstream.py --component" in s.get("run", ""))
        self.assertIn("configured", run_step["if"])
        # The checkout must not persist the workflow token as a git
        # auth extraheader: it would override the app-token remote
        # URL and pushes would go out as the read-only workflow token.
        checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_ready_workflow_checkout_does_not_persist_credentials(self):
        wf = self.load(READY_WORKFLOW)
        steps = wf["jobs"]["mark-ready"]["steps"]
        checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_ready_workflow_trigger_and_guards(self):
        wf = self.load(READY_WORKFLOW)
        triggers = wf.get(True) or wf.get("on")
        self.assertEqual(triggers["workflow_run"]["workflows"], ["CI"])
        self.assertEqual(triggers["workflow_run"]["branches"], ["upstream/**"])
        job = wf["jobs"]["mark-ready"]
        self.assertIn("conclusion == 'success'", job["if"])
        text = READY_WORKFLOW.read_text()
        self.assertIn("actions/create-github-app-token@v3", text)
        self.assertIn("--mark-ready", text)


class PrBodyShape(unittest.TestCase):
    def test_body_carries_policy_verdicts_and_provenance(self):
        plan = bump.Plan(
            "go2rtc", "bump", version="1.9.16", files=["docker/go2rtc-prebuilt.sh"],
            changes=["version: 1.9.15 -> 1.9.16"],
            provenance=["Release: https://example.test (published 2026-10-26)"],
            release=make_release("1.9.16"),
        )
        body = bump.build_pr_body("go2rtc", plan)
        self.assertIn("Documentation: not needed", body)
        self.assertIn("Wiki: not applicable", body)
        self.assertIn("Variables: not applicable", body)
        self.assertIn("Tests added: none", body)
        self.assertIn("Regression scoping: checked", body)
        self.assertIn("1.9.15 -> 1.9.16", body)
        self.assertIn("pending at open time", body)
        self.assertIn("newer final upstream release", body)

    def test_generated_body_passes_contribution_policy(self):
        """Bot bodies are PR bodies: the whole generated body must stay
        clean under scripts/check_contribution.py, including the Tests
        added and regression scoping verdicts."""
        plan = bump.Plan(
            "go2rtc", "bump", version="1.9.16", files=["docker/go2rtc-prebuilt.sh"],
            changes=["version: 1.9.15 -> 1.9.16"],
            provenance=["Release: https://example.test (published 2026-10-26)"],
            release=make_release("1.9.16"),
        )
        body = bump.build_pr_body("go2rtc", plan)
        policy_spec = importlib.util.spec_from_file_location(
            "check_contribution", ROOT / "scripts" / "check_contribution.py")
        policy = importlib.util.module_from_spec(policy_spec)
        policy_spec.loader.exec_module(policy)
        self.assertEqual([], policy.check_pr(body, plan.files))

    def test_prerelease_body_does_not_claim_a_final(self):
        plan = bump.Plan(
            "orca", "bump", version="2.5.0-rc2", files=["docker/orca-prebuilt.sh"],
            changes=["version: 2.5.0-rc -> 2.5.0-rc2"],
            provenance=["Release: https://example.test (published 2026-11-04)"],
            release=make_orca_release("2.5.0-rc2", prerelease=True),
        )
        body = bump.build_pr_body("orca", plan)
        self.assertIn("newer upstream prerelease", body)
        self.assertNotIn("newer final upstream release", body)
        self.assertNotIn("latest final upstream release", body)


if __name__ == "__main__":
    unittest.main()
