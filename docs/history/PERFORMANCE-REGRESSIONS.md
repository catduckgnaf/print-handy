# Performance regressions

Print Handy has slowed down for users more than once, and several of those
slowdowns came back in another form because the fix landed without a guard
that would notice a repeat. This file is the record: what was slow, why, how it
was fixed, what now guards it, and what it taught. Read it before you touch a
hot path (G-code scans, mesh parsing, the fit search, the slice pipeline), and
add an entry whenever a slowdown is reported or found (#1392).

## When something is slow

1. **Measure the real case, not a guess.** Every job folder has `TIMING.jsonl`
   (stage timestamps, slicer time split from Python time since #993), and the
   log lines carry the intake id. Write down the gap that grew, in seconds, and
   which stages bracket it. A worker's log lines are relayed when it returns,
   so read the per-day file log for when a step really started.
2. **Profile the artifact that was slow.** Copy the job folder to a scratch
   directory inside the running container and re-run the step under
   `python -m cProfile`, never on the live archive. Then time the hot functions
   alone without the profiler: cProfile inflates pure-Python loops 1.5x to 2x.
3. **Check which release added it.** Compare against the previous release's
   numbers for the same model. For CI timing data, normalize each run by its
   runner's own speed (the median ratio over the cases that did not change):
   hosted runners differ by about 1.5x, and that alone has passed for a
   regression (#1332) and hidden one (#1347, #1388).
4. **Fix the work, not the threshold.** Remove a repeated pass, a redundant
   parse, a list that is never read. Keep every verdict the same: pin the old
   behavior with a frozen copy of the removed code and an equivalence test.
5. **Guard it by work done, not only by time.** A timing gate on small mock
   cases cannot see a pass that only hurts on a 25 MB G-code. Count the work
   (passes over the file, parses of the archive, probes) in a test, so a repeat
   fails on any runner. Timing gates stay as the second line.
6. **Record it here.** Add an entry below with the numbers, the cause (say
   "unverified" when it is), the fix, the guard and the lesson. A re-recorded
   baseline that rises names its entry here in its `reason`.

## Ledger

Newest first. "Guard" is what fails if it comes back; "none" is a known gap.

### 2.3.2: the Ready question waited 26 s after OrcaSlicer on a large single-color job (#1392)
- **Seen:** a 25 MB, 1.16M-line single-color G-code (the lantern at the designer's 0.20 mm layers and gyroid infill): OrcaSlicer took 9.6 s, then 26 s passed before the Ready question.
- **Cause (profiled in the container):** post-slice Python passes over the file. The ZMod finalize ran twice: `slice_model` finalizes with the one-slot map built from `filament_slot`, and every single-color caller in `jobs/slice.py` then called `finalize_multicolor` with the same map, re-normalizing the whole file for identical bytes (5.1 s). `slice_guard.post_slice`'s bed check parsed every word of every move into a dict and kept a list of every point (10.0 s for the whole guard, 6.2 s of it the bed loop). Neither was new in 2.3.2; the designer's settings made the G-code larger, so every pass cost more.
- **Fix:** `finalize` returns early when the file already carries a valid checksum and exactly the map and color lines it would write; the bed loop reads only X and Y (4.7 s).
- **Guard:** `tests/pipeline/test_finalize_once_1392.py` (one normalization per single-color slice, byte equality with a full re-finalize, the bed verdicts against the frozen old loop).
- **Review correction:** the first-round early return trusted a file that carried a valid MD5 and Print Handy's own map lines. Both can be written by hand, so a crafted file skipped the start-heater normalization and the material and tool checks. The second round removes it: every finalize runs in full again, made cheap instead (1.7 s on the lantern, from 5.1 s), and an unchanged result is not rewritten. Guarded by the two crafted-file tests in `test_finalize_once_1392.py`, which fail on the first-round code.
- **Second round (same issue):** timed on the same G-code in the container, the passes after OrcaSlicer went from about 15 s to 3.6 s:
  - `post_slice` 8.5 s to 1.2 s. The bed loop moved to a Rust port in the bounds extension (`native/bounds/src/bed_scan.rs`, 4.7 s to 0.18 s), with the Python loop kept as the contract and fallback under the bounds parser's rules (ASCII only, version handshake, golden self-check). The settings regexes run only on comment lines with an `=`, the filament-ID search only when there are no IDs to check, the time-estimate search looks at both ends of the file first, and the tool scan reads only lines that start with a T or a blank.
  - The ZMod start-code checks (`native_startup.valid` and `normalize`) 0.9 s and 1.7 s to 0.4 s each: they built a list of every command line to read one `START_PRINT` and its neighbours.
  - `GcodeLines._scan` (offline verify) 1.1 s to about 0.2 s: a `^`-anchored regex is tried at every character; one led by a newline jumps from line to line.
  - The approved-baseline config read 0.8 s to 0.45 s (only comment lines with an `=`).
- **Guard (second round):** `tests/pipeline/test_native_bed_scan.py` (Rust and Python agree on every fixture and on fuzzed G-code; the dispatcher uses the extension when it is built; the time-estimate search never reads the whole file when the footer has it; tool scan and time check against the old whole-file regexes), `tests/pipeline/test_native_startup_1392.py` (the start-code checks against a frozen copy of the old code), and the existing `tests/core/test_gcode_lines_equivalence.py`.
- **Lesson:** large real artifacts expose costs the mock timing cases never see. Two callers each doing "the safe thing" made the same pass twice. Skipping a safety pass because a file looks already processed is a trust decision, not an optimization: make the pass cheap instead. Python's `re` scans about 20 MB/s when it has to try a match at every character: on a 25 MB file each whole-text `^` search is a second, so find candidate lines with plain string operations first.

### 2.3.2: shrink-question intake +0.27 s (#1378, accepted in #1388)
- **Seen:** `must-shrink` and `stl-must-shrink` rose from about 0.44 s to 0.71 s (about 1.5x after normalizing for runner speed).
- **Cause (isolated in #1396):** timed 5 rounds each on the trees before and after #1378 (must-shrink median 1.10 s to 1.31 s, stl-must-shrink 0.73 s to 0.95 s, all of it between the answer and the slice start) and profiled: #1378 added `_refresh_posed_clearance`, which re-runs the support-clearance analysis (`support_clearance.posed_report`, about 0.28 s on the case) for the pose the job prints. A shrink scales every gap, so the report made at intake for the full-size pose is wrong for it.
- **Fix:** none; accepted. It is a correctness fix from the #1373 review, it runs only when the pose changed, and the rasterizer is already vectorized.
- **Guard:** the gate's drift limit; the rise is under its 0.35 s floor.
- **Lesson:** an absolute slack floor hides slowdowns on short flows; normalize per runner to see them. Not every rise is a defect: name the work that was added and decide.

### 2.3.1: Discord-flow overhead +50% since 2.2.9 (#1349, cause #1343, fixed by #1352)
- **Seen:** islands-144 0.87 s to 1.35-1.42 s; witch-ghost 1.43 s to 1.78-1.93 s.
- **Cause (bisected, 12 samples per commit):** after #1343 moved the handlers, loading the plugin recompiled every handler (1.5 ms to 120 ms per load), mostly a harness cost.
- **Fix:** #1352 caches the compiled handler bodies.
- **Guard:** `test_no_case_stands_past_the_drift_warning_on_main`; the drift check refused the re-record that would have hidden it.

### 2.3.0: baselines recorded on a fast runner (#1347, reverted by #1350)
- **Seen:** main went red on four cases after the re-record.
- **Cause:** the source run landed on a runner about 30% faster.
- **Guard:** process: the runner CPU is recorded in every timing artifact, and the release checklist requires the release commit's run on a runner at baseline speed.

### 2.3.0: a timing "rise" that was runner hardware (#1332, #1335)
- **Seen:** the same commits fell into speed bands about 1.6x apart over 28 runs; every phase scaled together.
- **Fix:** `time_corpus.cpu_model()` records the runner CPU.
- **Lesson:** record the CPU and compare like with like before blaming code.

### 2.2.x: the speed plan (#1153; #1165, #1222, #1250, #1264, #1335)
- **Seen:** a linear scale search of up to 54 probes of 4-6 s each; 0.6-1.1 s of cold Python per heavy step; Web UI status running the doctor on every request; painted fit 10-17 s.
- **Fix:** bisected scale search (about 12 probes), lazy imports (printers import 601 ms to 61 ms), the warm heavy worker, a cached status, painted fit 4.5-5 s.
- **Guard:** `test_sizing.py::test_largest_fitting_scale_matches_the_linear_walk_with_far_fewer_probes`, `test_lazy_registry.py`, `test_working_signal.py`, `tests/perf/test_perf_gate_1153.py`, `tests/pipeline/test_slice_reads_1153.py` (the slice reads its G-code once for its checks).

### 2.2.5-2.2.7: re-records hid real slowdowns (#1142; #1139, #1170, #1188)
- **Seen:** mock first prompt 0.01 s to 0.13-0.39 s and real witch-ghost +62% (#943); assembly cases +20-30% (#1073), each re-recorded with a copied reason.
- **Cause:** a flat 0.5 s slack could not fire on sub-second cases. #943's mock rise was the intake reorder (by design), its real rise runner noise; #1073 was real, spread intake work, no single culprit.
- **Guard:** `timing_gate.py`: slack max(0.35 s, 10%), a fresh reason of 20+ characters on every raise, cumulative drift against `original` (warn at +25%, fail at +50%).

### 2.2.5: large-slice post-processing (#925, fixed by #929 and #931; #952)
- **Seen:** a 54 MB tree-support slice: about 58 s in Orca plus about 99 s in Python (finalize scan 25 s, verify 37 s, a duplicate verify at print-now 17.4 s, packaging 19.5 s).
- **Fix:** single-pass tool scans, a verified-artifact reparse skip at launch, head/tail metadata reads, zip level 1. #952 then found the live re-slice still slow because #925's proposed savings had been read as #929's shipped ones (checklist audit A11); #993 split slicer time from Python time in `TIMING.jsonl`.
- **Guard:** `tests/perf/test_perf_gate_1153.py::test_verify_scan_perf_gate` (synthetic plate, ratio based); the real-slicer `stl-large-plate` case (reporting only).
- **Lesson:** keep proposed savings apart from shipped ones; a large real-slicer case must count, not just report.

### 2.2.x: per-line regex recompilation on a huge plate (#742)
- **Seen:** a 347 MB, 12.5M-line plate: 238M `re._compile` calls (187 s) plus 125M enum unwraps.
- **Fix:** regexes compiled once.
- **Guard:** none at that scale.

### 2.2.x: AD5X "slow start" of 31-47 s (#739, #745)
- **Seen:** every start since 10-30 took 31 s or more; two prints were reported failed but printed.
- **Cause (printer logs):** screenless ZMod homes, runs KAMP and loads filament inside the start request (up to +239 s); the 30 s client timeout produced the "31 s". `_wait_printing` also missed the `3mf_print/` name.
- **Fix:** #745 and #739's progress messages; #691/#698 for the false failures.
- **Guard:** recorded-response replay tests; no timing guard. A slow start is often the printer, not Print Handy.

### 2.2.0: Orca hangs at the container PID limit (#631)
- **Seen:** "Orca fit probe timed out".
- **Cause (live):** thread pools sized to the host's 72 CPUs under `--cpus 2`; pids reached 256 of 256.
- **Fix:** pools sized to the cgroup quota (`core/resources.py`); a named `ResourceLimitError`.
- **Guard:** resources unit tests; no timing guard.

### 2.1.x: fit checks of 36 s and 280 s (#634, #601)
- **Seen:** a desk organizer fit check about 36 s; the Duplo fit about 280 s.
- **Cause (cProfile):** the 3MF re-parsed 45 times (89 s) and O(n*m) hull packing (69-138 s); no-op rearrange and split searches on a single body.
- **Fix:** #601 (41 s with real Orca), #634 (about 12 s).
- **Guard:** the mock `fit_check_s` timing, small cases only.

### 2.0.x: huge-mesh audit hang (#188)
- **Seen:** a 1.06M-triangle 3MF sat in the audit for 15+ minutes, then failed.
- **Cause (faulthandler):** `placement_candidates` re-rotated every vertex for about 1,750 candidates.
- **Fix:** #188 (27 s for that step) and a large-model notice.
- **Guard:** `tests/core/test_large_model_notice.py` only.

## Guards and what they cannot see

| Guard | Measures | Blind to |
|---|---|---|
| `discord-timing` (`tests/corpus/timing_gate.py`, `timing_baseline.json`) | mock-slicer intake: wall, first prompt, fit check, medians of 3 | real Orca time, large G-code, rises under 0.35 s, runner speed differences |
| `slicer-timing-real` (`timing_baseline_real_slicer.json`) | six real-Orca cases incl. `stl-large-plate` | reporting only; seconds of noise |
| `tests/perf/` | in-process ratio gates on synthetic meshes and plates, with work-done checks | end to end flow, real model sizes, container limits |
| Work-count tests (`test_slice_reads_1153.py`, `test_finalize_once_1392.py`, probe counts) | passes, reads and probes per operation | anything not counted |

Not guarded at all: printer start latency, production startup time, models over 1M triangles or G-code over 50 MB end to end, behavior under a container CPU or PID cap.
