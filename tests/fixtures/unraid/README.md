# Unraid Community Applications template fixture

`print-handy-ca.xml` is a byte-exact snapshot of the Community
Applications (CA) template `templates/print-handy.xml` in the
`catduckgnaf/catduck-unraid-templates` repository, taken from that
repository's `main` branch (commit
`9028daa14ddfbda6a85c0c71a7970a095b48b988`) on 2026-11-10 for issue
#1143.

`UnraidCaTemplateParityTests` in `tests/test_docker_release.py` parses
this snapshot and the in-repo template (`docker/unraid/print-handy.xml`)
and compares them. The test cannot read the CA repository from the
network in CI, so this snapshot is the honest stand-in: refreshing it
is a deliberate, reviewed act, and drift between the snapshot, the
in-repo template and the pinned differences fails the test.

## Refresh path

When the CA template changes (for example the pinned release tag bump
on a new release):

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

- `Repository`: the in-repo template pins the release's multi-arch
  index digest; the CA template names the release tag, with `Branch`
  entries for that tag, `nightly` and `develop`.
- Listing metadata: `Registry`, `TemplateURL` and `Icon` are filled
  only in the CA copy; `Support` is the GitHub issue tracker in-repo
  and the Unraid forum thread in CA.
- Paths: the CA copy keeps an explicit `Path` entry for each former
  image `VOLUME` folder (`state`, `hermes`, `archive`, `records`,
  `cache`, `spoolman`) plus `.config`, so installs created on images
  that still declared those volumes keep their data on the host. The
  in-repo template relies on the single Appdata path now that the
  image declares no `VOLUME`.
- The bundled/external Spoolman URL variable exists only in-repo.
- The `Overview` texts match apart from the closing docs-link
  sentence, and the `/data` and `/dev/dri` `Description` texts differ
  in wording while keeping the same safety content (the ownership
  command, and the empty-by-default device with its render-group
  note). The CA `Requires` text additionally carries the upgrade and
  recovery warnings, including the `dot-config` rename.
