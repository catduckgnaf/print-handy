"""Fetch the large-mesh corpus (#1790) into a local root, pinned by SHA-256.

The files are tens of MB each and are never committed or released (see each
case's case.toml for its license record). This downloads every [[file]] of
every case under tests/corpus/large_mesh/ through the same Printables route
the complex corpus uses (tests/corpus/fetch_complex_corpus.py: the GraphQL
getDownloadLink mutation production intake uses, no token) into

    <root>/<case>/<file name>

and refuses a file whose byte size or SHA-256 differs from the manifest.
An already fetched, matching file is kept.

Usage:
    python -m tests.corpus.large_mesh.fetch [--root DIR] [--only case,...]

The root defaults to $PRINT_HANDY_LARGE_MESH_CORPUS, else
~/print-handy-large-mesh-corpus.
"""
import argparse
import os
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def default_root():
    return Path(os.environ.get('PRINT_HANDY_LARGE_MESH_CORPUS')
                or Path.home() / 'print-handy-large-mesh-corpus')


def cases():
    """{case name: parsed case.toml} for every case folder."""
    found = {}
    for manifest in sorted(HERE.glob('*/case.toml')):
        found[manifest.parent.name] = tomllib.loads(manifest.read_text(encoding='utf-8'))
    return found


def fetch_case(name, case, root):
    from tests.corpus import fetch_complex_corpus as fetcher
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    for row in case['file']:
        dest = folder / row['name']
        if dest.is_file() and dest.stat().st_size == row['bytes'] and fetcher.sha256(dest) == row['sha256']:
            print(f'  ok (already fetched) {name}/{row["name"]}')
            continue
        if case['site'] != 'printables':
            raise RuntimeError(f'{name}: no fetch route for site {case["site"]!r}')
        url = fetcher.printables_link(row['site_file_id'], case['site_model_id'])
        fetcher.download(url, dest)
        got = dest.stat().st_size
        if got != row['bytes'] or fetcher.sha256(dest) != row['sha256']:
            dest.unlink()
            raise RuntimeError(f'{name}/{row["name"]}: fetched {got} bytes with a different SHA-256; '
                               'refusing a changed file')
        print(f'  fetched {name}/{row["name"]} ({got} bytes)')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=None)
    parser.add_argument('--only', default='')
    args = parser.parse_args(argv)
    root = args.root or default_root()
    wanted = {name for name in args.only.split(',') if name}
    for name, case in cases().items():
        if wanted and name not in wanted:
            continue
        print(f'{name} -> {root / name}')
        fetch_case(name, case, root)
    return 0


if __name__ == '__main__':
    sys.exit(main())
