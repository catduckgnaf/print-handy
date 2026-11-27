# Copies to Ready benchmark (#1779)

`elf_ready_bench.py` times the span Discord reports as "copies chosen to Ready" for one
MakerWorld 3MF. It drives the real `prepared_intake.prepare_link` flow: the real background
slice (Orca CLI, `jobs_slice.slice_job`), the speculative with-supports slice when the flow
launches it, the support coverage check and the Ready question. The site download, the
printer hardware record, the answers to every question and Moonraker (a loopback fake that
answers the slice-time printer check) are canned. Spoolman points at a closed loopback port.
Sockets to anything but loopback raise. It never uploads or starts a print: the run ends when
the Ready question is posted and is answered with its Cancel button.

## Inputs

- `--source`: the 3MF the intake would download.
- `--job-record`: the archived `JOB.json` of a real intake. It supplies the model inventory,
  the hardware record and the color decision. The color decision slot must be an integer.
  Elf shorts: slot 3, red PLA, Spoolman spool 24.
- `--copies` (default 2), `--out` (JSON report), `--workdir`, `--timeout`.

## Run it (Francium, never Dysprosium)

Orca is in the Print Handy image, so run in the image with the checkout mounted over `/app`
code and the network disabled:

```sh
B=$HOME/print-handy-test/<bench-dir>   # holds app/ (the checkout), in/source.3mf, in/JOB.json, out/
timeout 900 docker run --rm --network none --entrypoint sh -v $B:/b -w /b \
  -e PYTHONPATH=/b/app -e BENCH_HEAD=$(git -C <checkout> rev-parse --short HEAD) -e HOME=/tmp/benchhome \
  ghcr.io/catduckgnaf/print-handy:develop -c \
  "mkdir -p /tmp/benchhome && cd /b/app && python scripts/bench/elf_ready_bench.py \
   --source /b/in/source.3mf --job-record /b/in/JOB.json --out /b/out/run1.json --copies 2 \
   --workdir /tmp/work-run1"
```

Exit code 0 means a report with a Ready time was written.

## Report

- `copies_to_ready_s`: copies answer delivered to the Ready question posted. `link_to_ready_s`: link to Ready.
- `slices.<first|supports>`: `slicer_s` (Orca), `python_s` (Print Handy's own work),
  `prepare_s` (inventory + geometry + other prepare work), `python_subphases_s`, `wall_s`, `verify_s`.
- `variants`: whether the with-supports slice launched, why not when it did not (the designer's
  support setting kept, no floating-region warning), and whether its G-code is identical to the
  first slice's (comparison ignores timestamp and generator header lines).
- `coverage`: the preview support coverage check (`elapsed_s`), when it ran.
- `gcodes`: digests and the `enable_support` flag of each G-code, read from a copy of the job
  folder taken when Ready is posted (Cancel deletes the sliced files).
- `timing_stages`: every `TIMING.jsonl` stage of the run.

The timing varies by a few seconds between runs; compare two or more runs per head.
