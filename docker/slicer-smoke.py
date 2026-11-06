"""Offline in-image smoke: require the selected bundled Orca and slice a cube.

Run via the image entrypoint with a writable /data tmpfs and --network none.
No printer or Discord access. Fail if a selected binary or source identity is absent.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, "/app")
from print_handy import settings
from print_handy.core import slice_guard


def main():
    channel = os.environ["PRINT_HANDY_ORCA_CHANNEL"]
    # The preview channel was removed (Print Handy ships one bundled Orca):
    # 'preview' and its pre-#600 name 'alpha' mean the single stable build.
    # The entrypoint already normalizes them, and a direct run with an old
    # value gets the same treatment here.
    if channel in {"alpha", "preview"}:
        channel = "stable"
    assert channel == "stable"
    binary = Path(f"/opt/orca-{channel}/bin/orca-slicer")
    assert binary.is_file() and os.access(binary, os.X_OK), binary
    revision = Path(f"/orca-{channel}-source-revision").read_text().strip()
    assert len(revision) == 40
    # Resolve the single build's private libraries. The
    # image sets no global LD_LIBRARY_PATH (Orca's bundled FFmpeg 7.0 libav*
    # sonames would shadow the system ffmpeg's libraries), so name the
    # build's orca-runtime tree explicitly, mirroring upstream's env
    # helper (libexec/orca-slicer-env). An operator-provided value is kept
    # last, as an override.
    env = dict(os.environ)
    paths = [f"/opt/orca-{channel}/lib/orca-runtime", f"/opt/orca-{channel}/lib",
             f"/opt/orca-{channel}/bin"]
    if env.get("LD_LIBRARY_PATH"):
        paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(paths)
    closure = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=True, env=env)
    assert "not found" not in closure.stdout, closure.stdout
    # Use the official environment helper for prebuilt distributions.
    command_path = f"/usr/local/bin/orca-slicer-{channel}"
    config = settings.current()
    with tempfile.TemporaryDirectory(dir="/data/cache") as tmp:
        work = Path(tmp)
        # OrcaSlicer 2.5 writes 00000.log into its working directory at startup and
        # aborts when it cannot (the image's /app is not writable by the runtime
        # user), so every Orca invocation runs from the writable work directory.
        subprocess.run([command_path, "--help"], check=True, timeout=120, cwd=work)
        # Deterministic, locally generated closed cube; no model downloads.
        vertices = [(x, y, z) for x in (0, 20) for y in (0, 20) for z in (0, 20)]
        faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6),
                 (0, 1, 5), (0, 5, 4), (2, 6, 7), (2, 7, 3),
                 (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
        lines = ["solid cube"]
        for face in faces:
            lines += ["facet normal 0 0 0", "outer loop"]
            lines += ["vertex " + " ".join(map(str, vertices[i])) for i in face]
            lines += ["endloop", "endfacet"]
        model = work / "cube.stl"
        model.write_text("\n".join(lines + ["endsolid cube"]) + "\n")
        templates = config["orca"]["templates"]
        command = [command_path, "--load-settings",
                   f"{templates / 'machine.json'};{templates / 'process.json'}",
                   "--load-filaments", str(templates / "filament.json"),
                   "--arrange", "1", "--orient", "0", "--ensure-on-bed",
                   "--allow-newer-file", "--slice", "0", "--outputdir", str(work), str(model)]
        subprocess.run(command, check=True, timeout=900, cwd=work)
        gcode = work / "plate_1.gcode"
        assert gcode.is_file() and gcode.stat().st_size > 0
        result_json = work / "result.json"
        result = slice_guard.post_slice(
            gcode, tool_slots={0: 1},
            result_json=result_json if result_json.exists() else None,
        ).as_dict()
        assert result["status"] != "blocked", result
        print(json.dumps({"channel": channel, "revision": revision,
                          "gcode_bytes": gcode.stat().st_size, "guard": result}))


if __name__ == "__main__":
    main()
