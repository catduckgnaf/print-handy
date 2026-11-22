# Large-mesh corpus tier (#1790)

Real multi-MB sources that exercise intake on large meshes: the STL unit
question, `load_parts` and `combine`, timed per stage. The generated corpus
in `tests/corpus/cases/` tops out at about 100 KB per model, which never
showed that intake spent minutes in per-triangle Python loops.

Each case is a folder with a `case.toml`: source page, license record and
attribution, the files pinned by byte size and SHA-256, the measured intake
result (per-part faces, dimensions, topology, volume and area, the combined
objects and the SHA-256 of the combined `3D/3dmodel.model`), and stage
budgets for the native kernel.

The model files are **not committed and not released**. They are fetched
from their source, pinned by hash, into a root outside the repo:

```bash
python -m tests.corpus.large_mesh.fetch            # -> ~/print-handy-large-mesh-corpus
PRINT_HANDY_LARGE_MESH_CORPUS=~/print-handy-large-mesh-corpus \
  python -m pytest -s tests/corpus/large_mesh      # skips cleanly when not fetched
```

`PRINT_HANDY_BOUNDS_IMPL=python` runs the Python fallback instead of the
native kernel; both must give the same result and the same combined model
bytes. `PRINT_HANDY_LARGE_MESH_TIMING=<file>` appends the stage timings as
JSON lines, and `PRINT_HANDY_LARGE_MESH_REQUIRE=1` turns a missing file into
a failure.

In CI the `large-mesh` job of the weekly `model-corpus` workflow fetches the
files, builds the native kernel and runs the tier on both paths (budgets on
the native run only), with the timings in the job summary. It is not part of
the PR lane; `workflow_dispatch` with `large_mesh_only` runs it alone.

| Case | Files | Triangles | License record |
| :--- | :--- | ---: | :--- |
| `snacking-tongs` | 2 STL, 56 MB | 1,121,066 | MIT stated by the owner; the Printables API reports CC BY-NC-SA. Treated as not redistributable until settled. |
