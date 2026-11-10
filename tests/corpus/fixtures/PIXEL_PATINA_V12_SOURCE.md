# Pixel Patina V12 With Removable Supports

- File: `Pixel_Patina_V12_With_Removable_Supports.3mf`
- SHA256: `94bc8a786427812811ad38e5248695995c26f2ff90de6bcd6eaa8f22b4d769fd`
- Origin / Owner: catduck (the project maintainer)
- Authorization: Explicitly authorized by the owner on 2026-11-05 as a test fixture for Print Handy.
- Purpose: Multi-object assembly fixture featuring:
  1. `BLACK BODY` (object id 2, pindex 0)
  2. `BRONZE DETAILS` (object id 3, pindex 1)
  3. `REMOVABLE SUPPORTS (delete object to remove)` (object id 4, pindex 0)
- Characteristics:
  - Three plain-3MF objects without transforms in mesh coordinates.
  - Separate modeled support object positioned under an overhang.
  - Used across all three edit corpus shards to verify:
    - Intake, alignment preservation, single-color vs per-part slicing.
    - Edits (scale, shrink, rotate) keeping the three objects together.
    - Questions answered without edits, support toggles, and multi-step undo.
