# Unraid Community Applications template fixture

`print-handy-ca.xml` is a byte-exact snapshot of the Community
Applications (CA) template `templates/print-handy.xml` in the
`catduckgnaf/catduck-unraid-templates` repository, refreshed on
2026-11-24 for the 2.3.7 release checklist (#1870) from that repository's
main at `3edcf44fc0a9` (catduckgnaf/catduck-unraid-templates#50: the
`Requires` text names 2.3.7, not 2.4.0, as the first release that no
longer reads the `hermes` folder). Earlier refreshes: `8a30d15e41ab` on
2026-11-22 for issue #1817, the head of
catduckgnaf/catduck-unraid-templates#48 (drops the `/data/hermes` path and
adds the rename sentence to `Requires`), main head `d448e92bfb7b` on 2026-11-19 (#1143), and the
original snapshot for catduckgnaf/catduck-unraid-templates#38 (the
`:latest` change) on 2026-11-11.

`UnraidCaTemplateParityTests` in `tests/test_docker_release.py` parses
this snapshot and the in-repo template (`docker/unraid/print-handy.xml`)
and compares them. The test cannot read the CA repository from the
network in CI, so this snapshot is the honest stand-in: refreshing it
is a deliberate, reviewed act, and drift between the snapshot, the
in-repo template and the pinned differences fails the test.

## Refresh path

When the CA template changes (a release does not change it: both
templates follow `:latest`):

```bash
curl -fsSL https://raw.githubusercontent.com/catduckgnaf/catduck-unraid-templates/main/templates/print-handy.xml \
  -o tests/fixtures/unraid/print-handy-ca.xml
```

Then update the commit and date above, run
`tests/test_docker_release.py`, and fix either the in-repo template or
the pinned intentional differences in the test — never refresh the
snapshot silently to make a failure go away.

## Intentional differences the test pins

The two files are the same install surface, not the same file:

- `Repository`: identical, `ghcr.io/catduckgnaf/print-handy:latest`.
  Neither template pins a version tag or digest. The CA copy also
  offers `Branch` entries for `latest`, `nightly` and `develop`.
- Listing metadata: `Registry`, `TemplateURL` and `Icon` are filled
  only in the CA copy; `Support` is the GitHub issue tracker in-repo
  and the Unraid forum thread in CA.
- Paths: the CA copy keeps an explicit `Path` entry for each former
  image `VOLUME` folder (`state`, `archive`, `records`, `cache`,
  `spoolman`) plus `.config`, so installs created on images that still
  declared those volumes keep their data on the host. The in-repo
  template relies on the single Appdata path now that the image
  declares no `VOLUME`. Neither maps the pre-2.2.5 `/data/hermes`
  folder, which 2.4.0 no longer reads (#1817).
- Every in-repo variable, the Spoolman URL and the bundled-firmware
  override included, is also in the CA copy.
- The `Overview` texts match apart from the closing docs-link
  sentence, and the `/data` and `/dev/dri` `Description` texts differ
  in wording while keeping the same safety content (the ownership
  command, and the empty-by-default device with its render-group
  note). The CA `Requires` text additionally carries the upgrade and
  recovery warnings, including the `dot-config` rename and the
  `hermes` to `state` rename.
