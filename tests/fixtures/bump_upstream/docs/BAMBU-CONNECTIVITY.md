## HMS error translation (#306)

Status and readiness output decode Bambu's HMS faults. Print Handy's table (`print_handy/printers/bambulab/hms_data.json`) is produced by Print Handy's own generator, `scripts/bambu-hms.py`, from those files at the pinned Bambu Studio commit recorded in the table's provenance block (`b94b6da251f8d5cf2773f2a78aa87c613ea9a907`), with each text shortened to its first sentence.

- `bambulab/BambuStudio` @ `b94b6da251f8d5cf2773f2a78aa87c613ea9a907` — `resources/hms/hms_en_<prefix>.json` HMS/print-error texts, the primary source for Print Handy's own generated HMS table (#306).
