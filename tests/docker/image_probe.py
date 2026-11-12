"""Checks run INSIDE a Print Handy image by tests/docker/image_acceptance.sh. Standard library only.

    python3 /probe/image_probe.py payload | closure | slice | egress | ffmpeg | openscad

Each prints one `PROBE {json}` line and exits non-zero on failure. Mounted read-only as data; the
image's own /app is what is tested. It never contacts a printer, Discord or any other service.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ORCA = Path("/usr/bin/orca-slicer")
# The image runs `python3 -m ...` from WORKDIR /app; a script needs that root on its path explicitly.
sys.path.insert(0, "/app")


def _orca_env():
    """Environment for invoking the raw Orca binary (/usr/bin/orca-slicer ->
    the stable tree). The image sets no global LD_LIBRARY_PATH (Orca's
    bundled FFmpeg 7.0 libav* sonames would shadow the system ffmpeg's
    libraries), so the raw binary gets its private library path scoped to
    this invocation, mirroring upstream's env helper
    (libexec/orca-slicer-env). Wrapped launches (ORCA_COMMAND -> AppRun)
    set it themselves and do not need this."""
    env = dict(os.environ)
    paths = ["/opt/orca-stable/lib/orca-runtime", "/opt/orca-stable/bin"]
    if env.get("LD_LIBRARY_PATH"):
        paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(paths)
    return env


def report(check, ok, **details):
    print("PROBE " + json.dumps({"check": check, "ok": ok, **details}, sort_keys=True, default=str))
    return 0 if ok else 1


def payload():
    """Handlers, config, templates and plugins come from the image, not a mounted checkout."""
    from print_handy import printers, settings
    from print_handy.bot.router import PLUGIN, Router
    root = settings.ROOT
    data = settings.load()
    missing = [str(p) for p in (root / "config" / "docker.yaml", root / "config" / "docker.toml",
                                root / "plugins" / "print-feedback", Path(PLUGIN)) if not p.exists()]
    templates = {}
    for key, model in printers.MODELS.items():
        if model.generated_templates:  # Bambu: profiles come from the user's slicer at runtime; none ship
            continue
        folder = root / "print_handy" / "templates" / "orca" / model.templates
        templates[key] = all((folder / f"{n}.json").is_file() for n in ("machine", "process", "filament"))
    mounts = [line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    source_mounted = any(m == "/app" or m.startswith("/app/") for m in mounts)
    handlers = Router().handler_names()
    # site-packages holds only the dependency-layer stub of print_handy; a
    # process started outside /app must still import the working copy.
    elsewhere = subprocess.run([sys.executable, "-c", "import print_handy.bot.router, print_handy; "
                                "print(print_handy.__file__)"], capture_output=True, text=True, timeout=120, cwd="/")
    imported_from = (elsewhere.stdout.strip() or elsewhere.stderr.strip()[-300:])
    ok = (str(root) == "/app" and not missing and all(templates.values()) and not source_mounted
          and len(handlers) > 0 and not (root / "tests").exists()
          and elsewhere.returncode == 0 and imported_from.startswith("/app/print_handy/"))
    return report("payload", ok, root=root, missing=missing, templates=templates, handlers=len(handlers),
                  imported_from_elsewhere=imported_from,
                  source_mounted=source_mounted, config=os.environ.get("PRINT_HANDY_CONFIG"),
                  slicer=data["orca"]["command"])


def closure():
    """With Orca: every shared library resolves and --help runs. Without: the binary is absent."""
    info = Path("/orca-build-info")
    if not ORCA.exists():
        ok = not info.exists() and not Path("/orca-source-revision").exists()
        return report("closure", ok, orca=False)
    # Orca writes 00000.log into its working directory at startup and aborts
    # when it cannot (the image's /app is not writable by the runtime user),
    # so --help runs from a writable work directory under the cache mount,
    # like every other Orca invocation in the probe and in production.
    from print_handy import settings
    work = Path(tempfile.mkdtemp(prefix="probe-closure-", dir=settings.current()["paths"]["cache"]))
    try:
        env = _orca_env()
        ldd = subprocess.run(["ldd", str(ORCA)], capture_output=True, text=True, timeout=60, env=env)
        missing = [line.strip() for line in ldd.stdout.splitlines() if "not found" in line]
        help_run = subprocess.run([str(ORCA), "--help"], capture_output=True, text=True, timeout=300,
                                  cwd=work, env=env)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    first = (help_run.stdout or help_run.stderr).strip().splitlines()[:1]
    build = dict(line.split("=", 1) for line in info.read_text().splitlines() if "=" in line) if info.exists() else {}
    revision = Path("/orca-source-revision").read_text().strip() if Path("/orca-source-revision").exists() else ""
    ok = (ldd.returncode == 0 and not missing and help_run.returncode == 0
          and bool(revision) and build.get("revision") == revision)
    return report("closure", ok, orca=True, missing_libraries=missing, help_exit=help_run.returncode,
                  help_first_line=first, display=os.environ.get("DISPLAY"), build=build, revision=revision)


def _cube(path, size=20.0):
    v = [(x, y, z) for x in (0, size) for y in (0, size) for z in (0, size)]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    lines = ["solid cube"]
    for a, b, c in faces:
        lines += ["facet normal 0 0 0", " outer loop", *[f"  vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)],
                  " endloop", "endfacet"]
    path.write_text("\n".join(lines + ["endsolid cube"]) + "\n")
    return path


def slice_check():
    """With Orca: slice a 20 mm cube with the packaged templates and pass the G-code guards.
    Without: the slicer path refuses with FileNotFoundError and writes no G-code."""
    from print_handy import settings
    from print_handy.core import orca_adapter
    from print_handy.core import slice_guard as sg
    data = settings.current()
    work = Path(tempfile.mkdtemp(prefix="probe-slice-", dir=data["paths"]["cache"]))
    try:
        cube = _cube(work / "cube.stl")
        if not ORCA.exists():
            try:
                orca_adapter.orca_plate_capacity(cube, {}, copy_limit=1)
            except FileNotFoundError as exc:
                return report("slice", not list(work.rglob("*.gcode")), orca=False, refused=str(exc))
            return report("slice", False, orca=False, refused=None)
        templates = data["orca"]["templates"]
        machine, process, filament = (templates / f"{n}.json" for n in ("machine", "process", "filament"))
        out = work / "out"
        out.mkdir()
        command = [*orca_adapter.ORCA_COMMAND, "--load-settings", f"{machine};{process}",
                   "--load-filaments", str(filament), "--arrange", "1", "--orient", "0", "--ensure-on-bed",
                   "--allow-newer-file", "--slice", "0", "--outputdir", str(out), str(cube)]
        run = subprocess.run(command, capture_output=True, text=True, timeout=900, cwd=work)
        gcode = out / "plate_1.gcode"
        result = None
        if run.returncode == 0 and gcode.is_file():
            result_json = out / "result.json"
            result = sg.post_slice(gcode, tool_slots={0: 1},
                                   result_json=result_json if result_json.is_file() else None).as_dict()
        ok = bool(result) and result["status"] != "blocked"
        return report("slice", ok, orca=True, exit=run.returncode, gcode_bytes=gcode.stat().st_size if gcode.is_file() else 0,
                      guard=result, log_tail=(run.stdout + run.stderr)[-1500:] if not ok else "")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ffmpeg():
    """ffmpeg is a shipped runtime dependency (#373): the timelapse sizing,
    assembly and vertical-short paths resolve it from PATH as the runtime
    user. It must resolve, run, and list the encoders the [timelapse]
    encoder options rely on: libx264 (the cpu path) and h264_vaapi (the
    VAAPI stack's encoder; the device itself needs the host passthrough).
    The NVENC/QSV encoders are recorded, not required (#455): Debian ships
    h264_qsv on amd64 only, and neither can open without a granted GPU.
    """
    binary = shutil.which("ffmpeg")
    if not binary:
        return report("ffmpeg", False, binary=None)
    version = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=60)
    encoders = subprocess.run([binary, "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=60)
    listed = encoders.stdout if encoders.returncode == 0 else ""
    have = {name: (name in listed) for name in ("libx264", "h264_vaapi")}
    gpu_optional = {name: (name in listed) for name in ("h264_nvenc", "h264_qsv")}
    ok = version.returncode == 0 and all(have.values())
    return report("ffmpeg", ok, binary=binary,
                  version=(version.stdout or "").strip().splitlines()[:1], encoders=have,
                  gpu_optional_encoders=gpu_optional)


def openscad():
    """OpenSCAD is a shipped runtime dependency (#388): the CAD-split and
    AI-design paths resolve it from PATH as the runtime user. It must
    resolve, run, and report its version."""
    binary = shutil.which("openscad")
    if not binary:
        return report("openscad", False, binary=None)
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
    text = ((version.stdout or "") + (version.stderr or "")).strip()
    ok = version.returncode == 0 and "OpenSCAD" in text
    return report("openscad", ok, binary=binary, version=text.splitlines()[:1])


def egress():
    """The container must have no route out: no DNS answer and no TCP connection."""
    attempts = {}
    for host, port in (("discord.com", 443), ("1.1.1.1", 443)):
        try:
            socket.create_connection((host, port), timeout=5).close()
            attempts[host] = "connected"
        except OSError as exc:
            attempts[host] = type(exc).__name__
    return report("egress", all(v != "connected" for v in attempts.values()), attempts=attempts)


if __name__ == "__main__":
    checks = {"payload": payload, "closure": closure, "slice": slice_check, "egress": egress,
              "ffmpeg": ffmpeg, "openscad": openscad}
    if len(sys.argv) != 2 or sys.argv[1] not in checks:
        raise SystemExit(f"usage: image_probe.py {' | '.join(checks)}")
    sys.exit(checks[sys.argv[1]]())
