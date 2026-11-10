# Hardcoded Inventory: Printer, Slicing, and Material Constants

This inventory documents hardcoded constants across `print_handy` related to printer configuration, slicing profiles, nozzle specifications, materials, and motion parameters (Issue #59).

---

## 1. Nozzle Size and Profile Assumptions

| Location | Hardcoded Value | Issue / Impact | Target Solution (#59) | Status |
|---|---|---|---|---|
| `print_handy/core/orca_adapter.py:25` | `NOZZLE_MM = 0.4` | Slicing hard-fails if printer reports any nozzle diameter other than `0.4` mm. | Support nozzle sizes `0.2`, `0.25`, `0.4`, `0.6`, `0.8` mm; dynamically load/generate corresponding tuned profile. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/core/orca_adapter.py:415-416` | `if float(state['nozzle']['diameter_mm']) != NOZZLE_MM:` | Strict runtime equality check blocking any multi-size capability. | Check against supported sizes for printer model, and verify tuned profile exists. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/templates/orca/*/machine.json` | `"nozzle_diameter": ["0.4"]`, `"printer_variant": "0.4"` | Base templates only describe 0.4mm nozzles. | Provide per-size profiles or template overrides with valid layer height and retraction values. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/templates/orca/*/machine.json` | `"max_layer_height": ["0.28"]`, `"min_layer_height": ["0.08"]` | Fixed to 0.4 mm nozzle rules (e.g. 0.25mm needs 0.08-0.14mm; 0.6mm needs 0.15-0.42mm; 0.8mm needs 0.24-0.56mm). | Dynamic layer height clamping / profile selection based on active nozzle diameter. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/templates/orca/*/machine.json` | `"retraction_length": ["0.8"]` | Retraction tuned specifically for 0.4mm (0.25mm uses 1.0mm; 0.6mm uses 1.2mm; 0.8mm uses 1.5mm). | Nozzle-specific retraction values mapped from printer model/profile. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/templates/orca/*/process.json` | Line widths: outer wall `0.42`, inner wall `0.45`, infill `0.45`, top surface `0.42` | Extrusion line widths hardcoded for 0.4 mm nozzle. | Line widths scaled or mapped per nozzle diameter (e.g. 105-112% of nozzle diameter). | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |

---

## 2. Nozzle Type and Material Safety

| Location | Hardcoded Value | Issue / Impact | Target Solution (#59) | Status |
|---|---|---|---|---|
| `print_handy/templates/orca/*/machine.json` | `"nozzle_type": "hardened_steel"` | Slicer assumes hardened steel regardless of physical nozzle hardware. | Set `nozzle_type` dynamically based on configured nozzle type (`brass`, `hardened_steel`, `ruby`, `tungsten_carbide`, `diamond`). | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/core/slice_guard.py` | Missing abrasive check | No safety guard prevents abrasive filaments (CF, GF, glow-in-the-dark, wood, metal-fill) from being sliced or printed on soft brass nozzles. | Enforce guard in `slice_guard.py`: fail validation if abrasive material is paired with a brass nozzle. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/core/printer_state.py` | Default `type: "hardened_steel"` | Permissive string with no validation against standard nozzle types. | Strongly validate against canonical enum/types: `brass`, `hardened_steel`, `ruby`, `tungsten_carbide`, `diamond`. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/settings.py` | `nozzle_diameter_mm: float = 0.4` | `PrinterConfig` only holds scalar diameter; no nozzle type or supported matrix. | Expand configuration schema to include nozzle type and validation of supported sizes/types. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |

---

## 3. Printer Model Capabilities

| Location | Hardcoded Value | Issue / Impact | Target Solution (#59) | Status |
|---|---|---|---|---|
| `print_handy/printers/base.py` | `nozzle_diameter_mm = 0.4` | Default property on base printer class; no capability declaration. | Declare `supported_nozzle_sizes` and `supported_nozzle_types` on printer models. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/printers/flashforge/ad5x.py` | Defaults to 0.4mm | AD5M/AD5X ecosystem natively supports 0.25, 0.4, 0.6, 0.8 mm nozzles. | Declare official supported nozzle diameters `(0.25, 0.4, 0.6, 0.8)` and types (`stainless_steel`, `hardened_steel`). | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/printers/sovol/sv06.py` | Defaults to 0.4mm | Standard V6/volcano nozzles: 0.2, 0.4, 0.6, 0.8 mm. | Declare supported nozzle diameters `(0.2, 0.4, 0.6, 0.8)` and types (`brass`, `hardened_steel`, `ruby`, `tungsten_carbide`). | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |
| `print_handy/printers/klipper/generic.py` | Defaults to 0.4mm | Generic Klipper setup with variable hotends. | Configurable supported nozzles list. | Migrated in main PR 90; this row documents the original item and is not outstanding migration. |

---

## 4. Temperature, Flow, and Geometry Constants

| Location | Hardcoded Value | Issue / Impact | Target Solution (#59) | Status |
|---|---|---|---|---|
| `print_handy/core/orca_adapter.py:528-531` | `prime_tower_width: '45'`, `purge_in_prime_tower: '0'` | Prime tower dimensions fixed; larger nozzles (0.6, 0.8) may require larger tower width / volume. | Dynamic prime tower sizing based on nozzle diameter. | Migrated: per-nozzle profiles set prime tower width; configured geometry clearance and zero-default typed thermal offsets are retained with effective-temperature safety checks. |
| `print_handy/core/color_geometry.py` | Fixed nozzle clearance calculations | Assumes 0.4mm line width during prime tower spacing and object bounding boxes. | Parameterize with current nozzle diameter. | Migrated: per-nozzle profiles set prime tower width; configured geometry clearance and zero-default typed thermal offsets are retained with effective-temperature safety checks. |
| `print_handy/core/printer_state.py` | `temps_c: {"nozzle": 220, "bed": 60}` | Hardened steel has lower thermal conductivity than brass, requiring offset or flow limit adjustments. | Adjust flow rate and temperature compensation based on nozzle thermal characteristics. | Migrated: per-nozzle profiles set prime tower width; configured geometry clearance and zero-default typed thermal offsets are retained with effective-temperature safety checks. |
