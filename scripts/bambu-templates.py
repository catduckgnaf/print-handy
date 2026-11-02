#!/usr/bin/env python3
"""Generate a Bambu Lab printer's Orca templates from the operator's own OrcaSlicer installation.

    python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder> [--check]
    python3 scripts/bambu-templates.py --printer a1 --resources <...> --import-filament <preset.json> [--material PETG]
    python3 scripts/bambu-templates.py --printer a1 --remove-filament PETG

Print Handy ships no OrcaSlicer or Bambu Lab profiles. This reads the BBL vendor profiles from an installed
OrcaSlicer (the folder that holds profiles/BBL.json and profiles/BBL/; a Flatpak keeps it under
`flatpak info --show-location com.orcaslicer.OrcaSlicer`/files/share/OrcaSlicer/resources), resolves the
printer's profiles, refuses unless they hash to an identity recorded in print_handy/printers/bambulab/facts.py,
and writes them with Print Handy's overrides into <state>/orca-profiles/<printer> for the configured profile.
That includes one checked filament profile per material the printer lists (`orca_filaments`; A1 and P1S: PLA, PETG,
TPU, PVA); slicing picks the one for the loaded material. Nothing is downloaded and nothing is written into the
repository.

--check writes nothing: it reports whether the installed OrcaSlicer has a checked identity and whether the
generated folder (if any) is current. Exit 1 on any problem.

--import-filament registers the operator's own exported Bambu Studio / OrcaSlicer filament preset (#264
phase 2): the preset is flattened against the installed OrcaSlicer and refused unless its parent is one of the
printer's checked filament profiles, it sets only that parent's filament settings, keeps the parent's material
and filament G-code, and stays inside the model's recorded limits. The registered preset replaces the vendor
profile for its material until --remove-filament takes it away. Regenerating the templates keeps it.
"""
import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--printer", default="a1", help="printer profile module under print_handy/printers/bambulab")
    ap.add_argument("--resources", type=Path, help="the installed OrcaSlicer resources folder")
    ap.add_argument("--check", action="store_true", help="verify only; write nothing")
    ap.add_argument("--import-filament", type=Path, metavar="PRESET",
                    help="register an exported filament preset for its material")
    ap.add_argument("--material", help="the material --import-filament must be for (checked, not chosen)")
    ap.add_argument("--remove-filament", metavar="MATERIAL", help="remove the operator preset for a material")
    a = ap.parse_args()
    from print_handy import printers, settings
    from print_handy.printers.bambulab import profiles
    model = importlib.import_module(f"print_handy.printers.bambulab.{a.printer}").MODEL
    adapter = printers.get(model.key, settings.load(environ={**os.environ, "PRINT_HANDY__PRINTER__MODEL": model.key}))
    materials = profiles.filament_set(adapter) or {"default": adapter.fact("orca_profiles")["filament"]}
    checked = ", ".join(f"{m} ({name})" for m, name in materials.items())
    try:
        if a.remove_filament:
            if a.resources or a.check or a.import_filament:
                print("REFUSED: --remove-filament stands alone (no --resources, --check or --import-filament)",
                      file=sys.stderr)
                return 1
            entry = profiles.remove_filament(adapter, a.remove_filament)
            print(f"removed operator filament preset {entry['name']!r} for {entry['material']} from "
                  f"{adapter.pinned_templates_folder()}; {entry['material']} slices with the vendor profile "
                  f"{entry['parent']!r} again")
            return 0
        if not a.resources:
            print("REFUSED: --resources <OrcaSlicer resources folder> is required", file=sys.stderr)
            return 1
        if a.check and a.import_filament:
            # --check promises "writes nothing" (P2-131): refuse the
            # combination instead of importing first and never checking.
            print("REFUSED: --check and --import-filament cannot be combined "
                  "(--check writes nothing; run the import without --check)", file=sys.stderr)
            return 1
        if a.import_filament:
            entry = profiles.import_filament(adapter, a.resources, a.import_filament, a.material)
            print(f"registered operator filament preset {entry['name']!r} for {entry['material']} "
                  f"(over the checked {entry['parent']!r}) in {adapter.pinned_templates_folder()}; "
                  f"{entry['material']} now slices with it until it is removed "
                  f"(--remove-filament {entry['material']})")
            return 0
        if a.check:
            _, _, _, known = profiles.check_resources(adapter, a.resources)
            found = profiles.problems(adapter)
            for material in profiles.filament_set(adapter):
                try:
                    profiles.filament_template(adapter, material)
                except profiles.ProfileError as exc:
                    found.append(str(exc))
            presets = profiles.operator_presets(adapter)
            for material in sorted(presets):
                try:
                    profiles.operator_template(adapter, material)
                except profiles.ProfileError as exc:
                    found.append(str(exc))
            print(f"{a.resources}: {known['label']} matches for {checked}"
                  + (f"; operator presets: "
                     f"{', '.join(f'{m} ({presets[m]['name']})' for m in sorted(presets))}" if presets else "")
                  + (f"; generated folder: {'; '.join(dict.fromkeys(found))}" if found
                     else "; generated folder is current"))
            return 1 if found else 0
        known = profiles.install(adapter, a.resources)
    except profiles.ProfileError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    try:
        kept = profiles.operator_presets(adapter)
        kept_note = (f"; operator presets kept: "
                     f"{', '.join(f'{m} ({kept[m]['name']})' for m in sorted(kept))}" if kept else "")
    except profiles.ProfileError as exc:
        kept_note = f"; operator presets could not be verified: {exc}"
    print(f"wrote {model.label} Orca templates from {known['label']} to {adapter.pinned_templates_folder()}; "
          f"filament profiles: {checked}" + kept_note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
