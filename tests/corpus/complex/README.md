# Complex slicer corpus (#458)

Real, complicated models for slicer diversity — the opposite of the
generated corpus in `tests/corpus/cases/` (tiny CC0 primitives from
`make_models.py`). The catalogue, measured stats and licences for every case
live in [`tests/corpus/complex_cases.toml`](../complex_cases.toml); stats were
measured from the downloaded files themselves, not copied from listings
(where a listing number disagrees with the file — e.g. the Stanford dragon
listing's 871,414 triangles describes the original scan, while the
distributed STL measures 318,460 — the manifest records the measured value
and notes the difference).

## Storage decision

The files are large (7–69 MB each, ~258 MB for the public set) and are
**not committed to this repository**, and public cases are not decimated or
shrunk to fit: the full-complexity mesh is the point of the case.

- Public cases are published as the `corpus-v2` release of
  `catduckgnaf/print-buddy-corpus` (the same mechanism as the 171-model
  `corpus-v1`), as a tar with one folder per case plus `SHA256SUMS`, and the
  `model-corpus` workflow downloads and verifies both releases.
- Anyone can reproduce the same files with
  `tests/corpus/fetch_complex_corpus.py`, which downloads them pinned by the
  manifest's SHA256/byte size through the same eligible-site paths production
  intake uses (#462): Printables via its GraphQL `getDownloadLink` mutation
  (no token), NASA via its direct public link. A file whose hash or size has
  changed upstream is refused, not silently accepted.
- Private-tier cases (licence-flagged: CC BY-NC-SA and Standard Digital File
  License) are **never** committed, released or uploaded anywhere. They run
  locally only, from a separate private root; only their measured stats and
  run outcomes are recorded (in the manifest and on #458).

## Running

```bash
python tests/corpus/fetch_complex_corpus.py            # -> ~/print-handy-complex-corpus
COMPLEX_CORPUS_ROOT=~/print-handy-complex-corpus \
  python -m pytest tests/corpus/test_complex_corpus.py # manifest contract + hash check
COMPLEX_CORPUS_ROOT=~/print-handy-complex-corpus CORPUS_WORK=<disk dir> \
  python tests/corpus/run_complex_corpus.py            # flow runs, JSON results
```

`run_complex_corpus.py` drives each case through the fake-Discord harness
(mock slicer tier) with a printer state built from the case's manifest
target: the AD5X fixture state with printer name, bed and max height taken
from the printer registry (`print_handy/data/printers-registry/flashforge.toml`). 

**Honest limits of that mechanism:**

- The slicer vendor profile and the spool/slot layout in the state are still
  the fixture's (AD5X). Per-domain *geometry and height* are exercised;
  per-domain Orca/Bambu vendor profiles are not switched by the harness
  today, and slot counts are not remodelled for toolchangers or AMS units.
  Expectations therefore pin flow outcomes (started / scale / copies), not
  vendor-profile G-code.
- Real-Orca slicing of the fetched set is not part of per-PR CI: the cases
  also run in the weekly `model-corpus` workflow through its generic
  external-corpus flow (mock tier, default fixture state, like `corpus-v1`),
  and with their target states locally through the runner above. A case that has not been fetched is
  reported `NOT_FETCHED` / skipped with that reason — never silently dropped.
- One public case is `blocked-download` in the manifest (Smithsonian
  mammoth): the source offers no direct file link, its EDAN API requires a
  key this project does not hold, and its 3D API route is WAF-rejected. It
  stays in the manifest, with the evidence, until a maintainer with a
  Smithsonian API key fetches it through the official route.

## Private tier

```bash
python tests/corpus/fetch_complex_corpus.py --private-root ~/print-handy-private-corpus
COMPLEX_CORPUS_ROOT=~/print-handy-private-corpus CORPUS_WORK=<disk dir> \
  python tests/corpus/run_complex_corpus.py --tier private
```

Private cases (none of these files is ever committed, released or uploaded):

| Slug | Licence | Target | Why it is here |
| :--- | :--- | :--- | :--- |
| `colormix-chameleon` | CC BY-NC-SA | Prusa XL | 972,972 painted triangles, 5-tool |
| `dragon-statue` | Standard Digital File License | Flashforge AD5X (stock) | 32.9 MB STL, heavy supports |
| `polymaker-scientist` | Standard Digital File License | Bambu H2D | `blocked-download` (no stats measured) |
| `duplo-swing-two-seats` | CC BY-NC | Flashforge AD5X | Live 2.1.9 failure: per-part colours + "100% · rearrange 1 plate" |

The fetcher has a MakerWorld route: the same account-token API call
production intake uses (`print_handy.core.model_job.maker_download`, token
file `~/.config/bambu/token`). Without the token it stops with the product's
own "needs an account token" error; a MakerWorld robot check (HTTP 418) is
reported, never solved or worked around. In either case, place the file at
`<private root>/<slug>/model.3mf` by hand: the fetcher accepts a file that
matches the manifest's SHA256 and size without contacting the site, so
`fetch_complex_corpus.py --private-root <root> --only <slug>` then writes its
`case.toml` and `ATTRIBUTION`. No scrape or captcha workaround is used, there
or anywhere else in this corpus. The 16 Color Polymaker Scientist case stays
`blocked-download` until someone with the token fetches and measures it.

### Click rules and known failures

The harness answers each question with the **first** `[[click]]` rule that
matches it. `case_toml()` writes a case's own rules (`CASE_CLICKS` in
`fetch_complex_corpus.py`) **before** the generic ones, so a case can
override a generic answer, for example choosing per-part colours instead of
"One color for everything", or "100% · rearrange 1 plate" instead of
"Shrink to". A case without its own rules gets exactly the generic set, as
before.

A manifest case may carry `expected_failure`, a list of error substrings for
a recorded product bug. The runner reports `XFAIL` while one of them
reproduces, `XPASS` once the case passes (then delete `expected_failure`),
and plain `FAIL` for anything else. When a case fails, the runner appends the
receipt's `slice_error` / `fit_error` to the row's error, because the
expectation message itself only names the receipt status.

`duplo-swing-two-seats` carries one until the per-part + rearrange fix lands.
Its clicks repeat the live attempt: per-part colours with every part on slot 1
except double-frame on slot 4, then "100% · rearrange 1 plate". The mock tier
reproduces the live error (`part group does not fit the target bed without
cropping`), because the colour-geometry export that raises it is Python and
is not mocked; the real tier fails the same way. The slot split matters: when the parts go on three slots
(frame, seats and chains separately), each per-slot group fits on its own and
the flow instead slices three plates and asks "Which plate prints first?".
That ignores the rearrange plan as well, but raises no error. The fit search
for this 9-part model takes about 2.5 minutes, and a whole mock-tier run
about 4.7 minutes.

`run_complex_corpus.py --real-slicer` (or `PRINT_HANDY_REAL_SLICER=1`) runs
the harness's real tier, where the installed Orca CLI does the fit probes
and the slice.
