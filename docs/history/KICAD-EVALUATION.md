# KiCad 3D models as slicing inputs and fixtures (evaluation for #122)

**Recommendation: defer.** Print Handy should not accept KiCad boards or projects as Discord input, and should not add KiCad as a runtime, CI or test dependency now. Revisit only for a concrete PCB-shaped printing need (see [Follow-up scope](#follow-up-scope-if-revisited)). #118 can proceed independently.

The export path works. In a sandbox, a KiCad 9 board-only STL passed Print Handy's existing intake, fit probe, real OrcaSlicer slice and slice guard unchanged. But for fixtures it adds nothing that `tests/corpus/make_models.py` or the planned OpenSCAD fixtures (#118) cannot produce with fewer dependencies. It needs a 346 to 673 MiB toolchain, and its output geometry changed between the KiCad versions tested.

## Evidence labels

Every factual claim below carries one of these labels:

* **[observed]**: run and seen in the evaluation sandbox, with the command shown.
* **[docs]**: taken from the linked documentation or package metadata, not run here.
* **[inference]**: reasoning from observed or documented facts.
* **[pending maintainer]**: needs a run on Host A or Host B. Neither host was accessed. The check to run is in [Host availability](#3-host-availability-host-a-and-host-b).

## Four validation areas

| Area | Status | Where it was done |
| --- | --- | --- |
| 1. Export and geometry | Done **[observed]** | Ubuntu 24.04 sandbox: KiCad 7.0.11, and KiCad 9.0.3 in an Ubuntu 25.10 chroot |
| 2. Real slicer and Print Handy pipeline | Done **[observed]** | Same sandbox: OrcaSlicer 2.4.2 AppImage. Offline only, with no printer, Moonraker, Spoolman or Discord |
| 3. Host availability on Host A and Host B | **Not done; pending maintainer** | Neither host was inspected or contacted. A reproducible check is provided |
| 4. Physical print | **Not done** | No printer access, and the issue rules out live printer actions |

No Docker container was used.

## Tools in the sandbox

| Tool | Version | Source |
| --- | --- | --- |
| OS, Python | Ubuntu 24.04.5 LTS, Python 3.13.16 | Sandbox **[observed]** |
| KiCad (host) | 7.0.11+dfsg-1build4 | Ubuntu 24.04 `noble/universe`, installed with `apt-get install --no-install-recommends kicad`. `noble-updates` and `noble-backports` were enabled; `apt-cache madison kicad` shows no other version **[observed]** |
| KiCad (chroot) | 9.0.3+dfsg-1 | Ubuntu 25.10 `questing/universe`, installed the same way inside a `debootstrap --variant=minbase` chroot. The KiCad PPA package archive (`ppa.launchpadcontent.net`) and Flathub (`dl.flathub.org`) were refused by the sandbox proxy **[observed]** |
| OrcaSlicer | 2.4.2 | [`OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage`](https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.4.2), extracted with `--appimage-extract`. CI and production use the 2.4.2 Flathub build (`.github/workflows/ci.yml`); same version number, different packaging **[observed]** |
| trimesh, cascadio | 5.1.1, 0.1.1 | pip, in a throwaway venv, used only for the STEP-to-STL check **[observed]** |

### Representative board

The test board is a hand-written 50 × 30 mm two-layer board with four 3.2 mm mounting holes on `Edge.Cuts`. It is own work with no third-party geometry, and its full source is embedded in the [host check script](#3-host-availability-host A-and-host B). KiCad 7.0.11 and 9.0.3 both load it **[observed]**.

A second variant, `parts.kicad_pcb`, adds two footprints:

* `U1` uses an 8 × 6 × 4 mm block model at `${KIPRJMOD}/block.step`. The block was itself exported from a tiny KiCad board, so it is also own work.
* `R1` points at a stock-library model, `${KICAD9_3DMODEL_DIR}/Resistor_THT.3dshapes/R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal.step`. The library is not installed, so this model is missing on purpose.

## KiCad CLI 3D export formats

These are the `kicad-cli pcb export` subcommands that write 3D geometry.

| Format | KiCad 7.0.11 | KiCad 8.0 | KiCad 9.0.3 | Orca 2.4.2 CLI accepts it | Notes |
| --- | --- | --- | --- | --- | --- |
| STEP | yes **[observed]** | yes **[docs]** | yes **[observed]** | **no [observed]** | Header contains file name and timestamp, so repeated exports differ (seen on both 7.0.11 and 9.0.3) **[observed]** |
| VRML | not in CLI **[observed]** | yes **[docs]** | yes **[observed]** | **no [observed]** | `--units` defaults to `in` (9.0.3 `--help`) **[observed]** |
| GLB | not in CLI **[observed]** | yes **[docs]** | yes **[observed]** | **no [observed]** | Loaded bounds are 0.05 × 0.00151 × 0.03, so the file is in metres with Y up **[observed]**. Repeated exports differ **[observed]** |
| STL | not in CLI **[observed]** | not in CLI **[docs]** | yes **[observed]** | **yes [observed]** | ASCII, millimetres. Byte-identical across repeated 9.0.3 runs **[observed]** |
| PLY | not in CLI **[observed]** | not in CLI **[docs]** | yes **[observed]** | **no [observed]** | Millimetres, same frame as STL **[observed]** |
| BREP | not in CLI **[observed]** | not in CLI **[docs]** | yes **[observed]** | not tried | Open CASCADE native format |
| XAO | not in CLI **[observed]** | not in CLI **[docs]** | listed in `--help`, not run **[observed]** | not tried | Open CASCADE native format |

Sources: the 7.0.11 and 9.0.3 columns come from `kicad-cli pcb export --help` and the export runs below. The 8.0 column comes from the [KiCad 8.0 CLI reference](https://gitlab.com/kicad/services/kicad-doc/-/blob/8.0/src/cli/cli.adoc), which has sections for STEP, GLB and VRML export and none for STL, PLY, BREP or XAO. KiCad 8 was not run. The [9.0 reference](https://gitlab.com/kicad/services/kicad-doc/-/blob/9.0/src/cli/cli.adoc) agrees with the observed 9.0.3 `--help` output.

Print Handy only slices `.3mf` and `.stl`: `MODEL_FILE_EXTS` in `print_handy/core/prepared_intake.py`, and the guard in `model_job.py` that raises "only STL/3MF can be sliced". Orca 2.4.2's CLI refused every non-STL KiCad output (STEP from 7.0.11 and 9.0.3, plus PLY, GLB and WRL) with exit 250 and the same message **[observed]**:

```text
<file>: Unknown file format. Input file must have .stl, .obj, .amf(.xml) extension.
run found error, return -6, exit...
```

So the only direct path is KiCad 9 or newer, exporting STL **[observed for 9.0.3; docs for 8.0]**. KiCad 7 and 8 would need an extra STEP-to-STL converter.

## 1. Export and geometry [observed]

In these commands, KiCad 9.0.3 ran in the chroot with the work directory bind-mounted at `/work`:

```bash
kicad-cli version                                                                        # 7.0.11 | 9.0.3
kicad-cli pcb export step --board-only -o host7-board.step demo.kicad_pcb                # 7.0.11, exit 0
kicad-cli pcb export stl  --board-only -f -o k9-board.stl  demo.kicad_pcb                # 9.0.3, exit 0
kicad-cli pcb export step|glb|ply|brep --board-only -f -o k9-board.<ext> demo.kicad_pcb  # 9.0.3, exit 0
kicad-cli pcb export vrml --units mm -f -o k9-board.wrl demo.kicad_pcb                   # 9.0.3, exit 0
kicad-cli pcb export stl [--fuse-shapes] -f -o k9-parts.stl parts.kicad_pcb              # 9.0.3, exit 2 (R1 model missing)
kicad-cli pcb export stl --no-components --include-pads --include-soldermask \
  -f -o pads.stl parts.kicad_pcb                                                         # 9.0.3, exit 0
```

For the KiCad 7 path, STEP was converted to STL with trimesh's cascadio loader. The loaded mesh reports `units == "meters"`, so without the `× 1000` the plate comes out as 0.05 × 0.03 units **[observed]**.

```python
mesh = trimesh.load("host7-board.step", force="mesh")
mesh.apply_scale(1000.0); mesh.merge_vertices(); mesh.export("host7-board-cascadio.stl")
```

Each STL was read with Print Handy's own `orca_adapter.read_stl_triangles`. A small script in the session scratchpad (not committed) then computed:

* bounds and signed volume;
* directed edges with no matching opposite edge, which indicate holes or flipped faces;
* connected shells, found by grouping triangles that share vertices.

| Mesh | Triangles | Bounds (mm) | Volume (mm³) | Unpaired edges | Shells |
| --- | --- | --- | --- | --- | --- |
| 9.0.3 `--board-only` STL | 412 | x 0–50, y −30–0, z 0–**1.51** | 2216.98 | 0 | 1 |
| 7.0.11 STEP → cascadio STL | 668 | x 0–50, y −30–0, z 0–**1.60** | 2348.74 | 0 | 1 |
| 9.0.3 STEP → cascadio STL | 668 | x 0–50, y −30–0, z 0–1.51 | 2216.62 | 0 | 1 |
| 9.0.3 parts STL, with or without `--fuse-shapes` | 724 | x 0–50, y −30–0, z 0–5.505 | 2401.98 | 0 | **2**: board z 0–1.51, block z 1.595–5.505 |
| 9.0.3 `--include-pads --include-soldermask` | 2824 | z −0.05–1.56 | n/a | **360** | 6, including zero-thickness mask faces at z −0.05 and 1.56 |

Findings:

1. **Board-only geometry is clean.** It is one closed, consistently wound shell **[observed]**. For a 1.51 mm plate with four 3.2 mm holes, the expected volume is 50 × 30 × 1.51 − 4π(1.6²)(1.51) ≈ 2216.4 mm³, which matches **[inference]**. Repeated 9.0.3 STL exports are byte-identical (sha256 `724ffd20a813bcca99ace5d9edfd2bc24de9fe26c4f386a71ee2887975993645`) **[observed]**.
2. **Board thickness depends on the KiCad version and the declared layers.**
   * With 7.0.11 STEP, the body is the nominal 1.60 mm **[observed]**.
   * With 9.0.3, the body is 1.51 mm. With `--include-pads`, copper appears outside it (z −0.04 to 1.55) **[observed]**. So the 9.0.3 body appears to be the dielectric only: 1.6 − 2 × 0.035 copper − 2 × 0.01 mask **[inference]**.
   * The same outline with the mask layers removed from the layer list exported as 1.53 mm (1.6 − 2 × 0.035) **[observed]**.

   A fixture regenerated on another KiCad version could change shape, so it would have to be committed as a mesh **[inference]**.
3. **Y is flipped.** KiCad's Y-down board space comes out as STL and PLY Y −30–0, while GLB is Y-up in metres **[observed]**. Print Handy re-centres STL input on the bed, and the pipeline runs below accepted it **[observed]**.
4. **Component exports are not printable solids.** The block's bottom is at z 1.595, 0.085 mm above the 1.51 mm body, so the mesh has two disjoint shells. `--fuse-shapes` changed nothing **[observed]**. The gap probably comes from KiCad placing models on the copper and mask surface rather than the dielectric **[inference]**. `--include-soldermask` adds zero-thickness open faces (360 unpaired edges), which is not a printable solid **[observed]**.
5. **Missing models are dropped and exports still succeed.**
   * 9.0.3 prints `Could not add 3D model for R1.` and `File not found: …`, exits **2**, and still writes the STL without R1 **[observed]**.
   * 7.0.11 printed `Could not add 3D model to U1.` and `File not found:` with an empty path, exited **0**, and wrote the STEP **[observed]**. That run had no `.kicad_pro` next to the board.
   * With 9.0.3, `${KIPRJMOD}` resolved only after a `.kicad_pro` existed next to the board. Before that, U1 was dropped too **[observed]**.
   * Pointing U1's model at an absolute path outside the project (`/work/block.step`) worked: the exporter read that file and embedded its geometry **[observed]**.

## 2. Real slicer and Print Handy pipeline [observed]

**Raw Orca CLI**, using the same flags as `tests/slicer/test_orca_real.py`, with `T=print_handy/templates/orca/flashforge_ad5x`. The binary was the extracted AppImage's `AppRun`, run under `xvfb-run -a`.

```bash
AppRun --load-settings "$T/machine.json;$T/process.json" --load-filaments "$T/filament.json" \
  --arrange 1 --orient 0 --ensure-on-bed --allow-newer-file --slice 0 --outputdir "$OUT" <input>
```

| Input | Exit | G-code | Layers | Filament | Estimated time |
| --- | --- | --- | --- | --- | --- |
| 9.0.3 board STL | 0 | `plate_1.gcode` | 6 | 2.89 g | 4 m 44 s |
| 9.0.3 parts STL (two shells) | 0 | `plate_1.gcode` | 23 | 3.05 g | 6 m 19 s |
| `.step` (7.0.11 and 9.0.3), `.ply`, `.glb`, `.wrl` | 250 | none | | | |

The AD5X profile slices at 0.24 mm layers, so the 1.51 mm plate became 6 layers **[observed]**: 1.44 mm, a 0.07 mm loss **[inference]**.

**Print Handy pipeline.** Each mesh was saved as a case in a private corpus outside the checkout (`PRINT_HANDY_PRIVATE_CORPUS`, with `CI` unset) and run through `tests/corpus/test_corpus.py::test_discord_flow`. That test exercises the real Discord intake hook, colour and copies prompts, fit probe, `BackgroundSlice`, slice guard and `print_next` start gates against the stub print-now script. Each case expected `source_kind = ".stl"`, `source_size_mm = [50, 30, z]`, one G-code, scale 1.0, supports off and one slot.

| Case | Mock tier (fake Orca) | Real tier (Orca 2.4.2) |
| --- | --- | --- |
| `kicad9-board-stl` | passed | passed |
| `kicad7-step-to-stl` | passed | passed |
| `kicad9-parts-stl` | passed | passed |

```bash
env -u CI PRINT_HANDY_PRIVATE_CORPUS=$PRIVATE python3 -m pytest -q tests/corpus/test_corpus.py::test_discord_flow -k kicad
env -u CI PATH=$SHIM:$PATH PRINT_HANDY_PRIVATE_CORPUS=$PRIVATE PRINT_HANDY_REAL_SLICER=1 \
  xvfb-run -a python3 -m pytest -v -s tests/corpus/test_corpus.py::test_discord_flow -k kicad
```

`tests/corpus/conftest.py` pins the home command `flatpak run com.orcaslicer.OrcaSlicer`. Flathub was unreachable, so `$SHIM` held a five-line `flatpak` shell script that runs the extracted AppImage for exactly `flatpak run com.orcaslicer.OrcaSlicer …` and exits 1 for anything else.

Control runs at upstream `0c3c0c1` in the same setup, all **[observed]**:

* Offline suite: 1195 passed, 36 skipped.
* `tests/corpus` real tier: 72 passed.
* `PRINT_HANDY_ORCA_CMD=<AppRun> PRINT_HANDY_REAL_SLICER=1 pytest tests/slicer`: 10 passed.

The two-shell parts mesh also passed. So the pipeline accepts what KiCad produces even when the result is not a sensible print. That is a reason not to feed it component exports, not evidence that they print well **[inference]**.

## 3. Host availability: Host A and Host B

**Nothing here was observed on either host.** This evaluation had no access to them, and no claim is made about their OS, installed KiCad or Flatpak setup. Because the recommendation is to defer, nothing needs installing on them now.

Known from outside the hosts:

| Route to KiCad 9 or newer | Status |
| --- | --- |
| Ubuntu 24.04 archive | Only 7.0.11, which has no STL export **[observed]** |
| Ubuntu 25.10 archive | 9.0.3+dfsg-1 **[observed]** |
| `ppa:kicad/kicad-9.0-releases` | Its [package list](https://launchpad.net/~kicad/+archive/ubuntu/kicad-9.0-releases/+packages) shows `kicad 9.0.9~ubuntu24.04.1` and builds for other Ubuntu series **[docs]**. Not installed here: the package archive was blocked |
| Flathub KiCad | Not reachable from the sandbox; app ID and `flatpak run --command=kicad-cli` usage unverified **[docs not checked]**. If Flatpak is used, expect the same private `/tmp` as the Orca Flatpak, so write under the checkout **[inference]** |

The **host check** below is self-contained and read-only apart from `.slicer-work/kicad-check/`, which is gitignored. Run it from the repository root on each host. It prints the OS and KiCad version, exports the representative board to STL, and reads the STL back with Print Handy's own reader. It does not touch the printer, Moonraker, Spoolman or Discord.

```bash
# Run from the print-handy checkout root. Writes only under .slicer-work/kicad-check/.
KICAD_CLI=${KICAD_CLI:-kicad-cli}   # Flatpak (unverified): KICAD_CLI="flatpak run --command=kicad-cli org.kicad.KiCad"
out="$PWD/.slicer-work/kicad-check"; mkdir -p "$out"
echo "host=$(hostname) os=$(. /etc/os-release; echo "$PRETTY_NAME")"
echo "kicad-cli version: $($KICAD_CLI version 2>&1 || echo 'not installed')"
cat > "$out/demo.kicad_pcb" <<'EOF'
(kicad_pcb (version 20221018) (generator pcbnew)
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_rect (start 0 0) (end 50 30) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (gr_circle (center 4 4) (end 5.6 4) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (gr_circle (center 46 4) (end 47.6 4) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (gr_circle (center 4 26) (end 5.6 26) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (gr_circle (center 46 26) (end 47.6 26) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
)
EOF
if $KICAD_CLI pcb export stl --board-only -f -o "$out/demo.stl" "$out/demo.kicad_pcb" >"$out/export.log" 2>&1; then
  python3 -c 'import sys; from print_handy.core.orca_adapter import read_stl_triangles as r
t = r(sys.argv[1]); p = [v for x in t for v in x]
print("native STL export: ok,", len(t), "triangles, bounds", [(round(min(q[a] for q in p), 2), round(max(q[a] for q in p), 2)) for a in range(3)])' "$out/demo.stl"
else
  echo "native STL export: unavailable (exit $?; needs KiCad 9 or newer, see $out/export.log)"
fi
```

Output in the sandbox **[observed]**:

```text
kicad-cli version: 7.0.11
native STL export: unavailable (exit 1; needs KiCad 9 or newer, see …/export.log)

kicad-cli version: 9.0.3
native STL export: ok, 412 triangles, bounds [(0.0, 50.0), (-30.0, -0.0), (0.0, 1.51)]
```

| Host | OS | `kicad-cli version` | Native STL export | Result |
| --- | --- | --- | --- | --- |
| Host A | pending maintainer | pending maintainer | pending maintainer | pending maintainer |
| Host B | pending maintainer | pending maintainer | pending maintainer | pending maintainer |

"Not installed" or "unavailable" is a valid result. It only matters if KiCad support is revisited.

## 4. Physical print

Not tested. No claim is made about how the plate prints, bed adhesion of a 1.5 mm part, or hole accuracy.

## Dependencies and licensing

* **Install size [observed].** These figures are sums of dpkg `Installed-Size` for the packages apt reported as newly installed, in MiB.
  * `--no-install-recommends kicad` added 27 packages and 346 MiB on the Ubuntu 24.04 sandbox, which already had many GUI libraries. On a minimal Ubuntu 25.10 chroot it added 202 packages and 673 MiB.
  * The `kicad` package alone is 128 MiB (9.0.3) or 143 MiB (7.0.11).
  * The stock 3D model package, `kicad-packages3d` 7.0.11-1, lists `Installed-Size` 5,709,285 KiB, about 5.4 GiB **[docs: apt metadata]**. It was not installed.
* **STEP converter for KiCad 7 and 8.**
  * trimesh and cascadio both declare the MIT licence in their pip metadata **[observed]**.
  * The cascadio wheel is about 99 MiB and bundles Open CASCADE 8.0.1 shared libraries (`libTK*.so.8.0.1`), but its `dist-info/licenses` holds only the MIT file **[observed]**.
  * Open CASCADE itself is LGPL-2.1 with an additional exception **[docs, not checked here]**. That would only matter if Print Handy redistributed the wheel.
  * FreeCAD or gmsh could also do the conversion; they were not tried.
* **KiCad itself.** The Debian copyright file in the 9.0.3 package lists GPL-3+ for KiCad **[observed]**. The GPL covers KiCad's code, not the designs users make or the files they export, and Print Handy would not redistribute KiCad **[inference; not legal advice]**.
* **KiCad libraries** (symbols, footprints, 3D models) use CC-BY-SA 4.0 with an exception that waives article 3 "with respect to these designs and any generated files which use data provided as part of the 'Licensed Material'". The exception does not apply to redistributing the libraries themselves as a collection ([kicad-packages3D `LICENSE.md`](https://gitlab.com/kicad/libraries/kicad-packages3D/-/blob/master/LICENSE.md), fetched during the evaluation) **[docs]**.
  * A board exported with stock models is therefore probably redistributable **[inference]**. But no label for it is accepted by the corpus allowlist (`PUBLIC_LICENSE` in `tests/corpus/test_corpus.py`) **[observed]**.
  * Board-only exports of an own-work board contain no library data and fit the existing `own work` label **[inference]**.
* **Third-party example boards** were not reviewed. Own-work boards avoid the question.

## Intake options compared

| Option | Verdict | Why |
| --- | --- | --- |
| Accept `.kicad_pcb` or a project in Discord and export on the bot host | **Reject** | It needs KiCad 9 or newer on the bot host, which the default Ubuntu 24.04 archive does not provide **[observed]**. Missing models still produce an STL **[observed]**. Model paths can point at arbitrary local files, which the exporter reads **[observed]**. It would run a large C++ parser on untrusted uploads, and Print Handy would have no bound on export time or size **[inference]**. |
| Accept exported STEP, VRML, GLB or PLY | **Reject** | Orca 2.4.2's CLI accepts none of them **[observed]**. Supporting them means adding a CAD kernel to intake, with unit traps: GLB and cascadio output in metres, VRML in inches by default **[observed]**. |
| Users export STL from KiCad 9 themselves and upload it | **Already supported** | A KiCad 9 STL went through the existing STL intake end to end **[observed]**. Nothing to build. |
| Use KiCad offline to generate test fixtures | **Defer** | It works **[observed]**. But a board-only export is a thin extruded outline with holes, which the stdlib generator `tests/corpus/make_models.py` could also produce without a 346+ MiB dependency **[inference]**. Component exports are multi-shell **[observed]**, and stock models come from a ~5.4 GiB package **[docs]**. |

## Compared with OpenSCAD fixtures (#118)

| | KiCad | OpenSCAD |
| --- | --- | --- |
| Purpose | PCB layout; 3D output is a by-product | Parametric solid modelling |
| Direct STL from the CLI | 9.0.3 yes, 7.0.11 no **[observed]**; 8.0 no **[docs]** | `openscad -o out.stl in.scad` **[docs, not run here]** |
| Ubuntu 24.04 `apt` | 7.0.11 **[observed]** | 2021.01-6build4, `Installed-Size` 9,838 KiB **[observed: apt metadata]** |
| Shapes it can express | Extruded board outlines; components only through external models **[observed]** | Arbitrary CSG, suited to fit and fail cases for #83 **[docs]** |
| Output stable across versions | No: 1.60 mm with 7.0.11, 1.51 mm with 9.0.3 **[observed]** | Not checked; #118 should verify |

OpenSCAD looks like the better fixture tool **[inference]**. Two lessons from this evaluation apply to #118:

* Commit the exported mesh next to the source and compare against it, so a toolchain upgrade cannot silently change a fixture.
* Check the tool version and fail with a clear message when the tool is missing.

## Follow-up scope if revisited

Reopen only for a concrete need, such as test-fitting a printed enclosure or spacer against a real board outline. Keep the scope bounded:

1. Generate a board-only STL with KiCad 9 or newer, offline, from a script under `scripts/`. Never at runtime, never from Discord uploads, never in CI.
2. Commit the own-work `.kicad_pcb` and the exported `model.stl` together as a corpus case with `license = "own work"`, and record the KiCad version and sha256 in `case.toml`.
3. Make the export script fail on any `Could not add 3D model` line or non-zero exit, and reject meshes with more than one shell or with unpaired edges.
4. Record the host check results for Host A and Host B first.
5. Add no new intake formats and no runtime dependency.

## Reproduction notes

* **KiCad 9 chroot.**
  * `debootstrap --variant=minbase --components=main,universe questing /opt/kicad9-root http://archive.ubuntu.com/ubuntu`, then `apt-get install --no-install-recommends kicad` inside it.
  * `debootstrap` 1.0.134ubuntu2 on Ubuntu 24.04 has no `questing` script, so a `questing → gutsy` symlink was added in `/usr/share/debootstrap/scripts`.
  * Board files were bind-mounted into the chroot.
* **Orca.** The AppImage needs `libwebkit2gtk-4.1-0` and runs headless under `xvfb-run -a`.
* **Generated files.** Meshes, G-code and the private corpus stayed in the session scratchpad and the gitignored `.slicer-work/`. Nothing generated is committed. This PR changes only this document.
