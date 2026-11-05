"""Backup and restore steps of the release lifecycle gate (tests/docker/lifecycle_gate.sh).

    python3 lifecycle_backup.py backup  SRC BACKUP_DIR
    python3 lifecycle_backup.py restore BACKUP_DIR DST

backup copies the stopped data tree SRC to BACKUP_DIR/tree. Every *.db file is copied with the
SQLite backup API instead of a raw file copy, stale -wal/-shm files are dropped, PRAGMA
integrity_check must answer ok, and BACKUP_DIR/SHA256SUMS lists every file in the copy.
restore copies BACKUP_DIR/tree into DST and checks every file against that manifest.

Every SQLite connection is closed before the manifest is written: a WAL-mode database leaves
-wal and -shm files next to an open connection, and those vanish when it closes, so hashing them
would produce a manifest no restore can match.
"""

import contextlib
import hashlib
import os
import shutil
import sqlite3
import sys

SIDE_FILES = ("*.db-wal", "*.db-shm", "*.db-journal")


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def backup(src, backup_dir):
    dst = os.path.join(backup_dir, "tree")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SIDE_FILES))
    dbs = 0
    for root, _, names in os.walk(src):
        for name in names:
            if not name.endswith(".db"):
                continue
            rel = os.path.relpath(os.path.join(root, name), src)
            os.remove(os.path.join(dst, rel))
            with contextlib.closing(sqlite3.connect(os.path.join(src, rel))) as a, \
                    contextlib.closing(sqlite3.connect(os.path.join(dst, rel))) as b:
                a.backup(b)
                assert b.execute("PRAGMA integrity_check").fetchone()[0] == "ok", rel
            dbs += 1
    lines = []
    for root, _, names in os.walk(dst):
        for name in names:
            path = os.path.join(root, name)
            lines.append((os.path.relpath(path, dst), _sha256(path)))
    with open(os.path.join(backup_dir, "SHA256SUMS"), "w") as fh:
        fh.writelines(f"{digest}  {rel}\n" for rel, digest in sorted(lines))
    return dbs, len(lines)


def restore(backup_dir, dst):
    shutil.copytree(os.path.join(backup_dir, "tree"), dst, dirs_exist_ok=True)
    count = 0
    with open(os.path.join(backup_dir, "SHA256SUMS")) as fh:
        for line in fh:
            digest, rel = line.rstrip("\n").split("  ", 1)
            assert _sha256(os.path.join(dst, rel)) == digest, rel
            count += 1
    return count


def main(argv):
    if len(argv) == 3 and argv[0] == "backup":
        dbs, files = backup(argv[1], argv[2])
        assert dbs >= 2, f"expected the launch DB and spoolman.db, found {dbs}"
        print(f"{dbs} SQLite files integrity ok; {files} files in manifest")
    elif len(argv) == 3 and argv[0] == "restore":
        print(f"restored and matched the manifest ({restore(argv[1], argv[2])} files)")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
