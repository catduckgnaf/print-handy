import json
from pathlib import Path

import pytest

from print_handy.core import projects
from print_handy.jobs import store


def test_source_checkpoint_persists_authored_identity_scope_and_no_approval(tmp_path):
    folder = tmp_path / "archive" / "intake-a"
    folder.mkdir(parents=True)
    source = folder / "source.3mf"
    source.write_bytes(b"source fixture")
    job = {"downloaded_file": str(source), "title": "Painted model"}
    manifest = {
        "source_sha256": "a" * 64,
        "plates": [{"id": "82", "name": "Cover", "object_ids": [3]}],
        "parts": [{"key": "part:lid", "name": "Authored Lid", "source_extruder": 2, "object_id": 3}],
        "painted": True,
        "painted_color": "#123456",
    }
    checkpoint = projects.source_checkpoint(job, manifest, {
        "platform": "discord", "requester_id": "u1", "channel_id": "c1", "printer_id": "p1"})
    job["source_checkpoint"] = checkpoint
    store.write_job(folder, job)

    # Fresh read models a service restart; scope, source revision and authored
    # names/paint survive, but no decision or launch authorization is reusable.
    restored = json.loads((folder / "JOB.json").read_text())["source_checkpoint"]
    assert restored["schema"] == "source_checkpoint_v1"
    assert restored["source_sha256"] == "a" * 64
    assert restored["request_scope"] == {
        "platform": "discord", "requester_id": "u1", "channel_id": "c1", "printer_id": "p1"}
    assert restored["authored_plates"] == [{"id": "82", "name": "Cover", "object_ids": [3]}]
    assert restored["authored_parts"] == [{"key": "part:lid", "name": "Authored Lid",
                                            "source_extruder": 2, "object_id": 3}]
    assert restored["color_manifest"]["painted_color"] == "#123456"
    assert restored["checkpoint"] == "source_inventoried"
    assert restored["launch_approval"] is None
    assert "multipart_plan" not in restored


def test_scoped_checkpoint_lookup_survives_restart_and_rejects_changed_source(tmp_path, monkeypatch):
    from print_handy.core import model_job

    folder = tmp_path / "archive" / "intake-a"
    folder.mkdir(parents=True)
    source = folder / "source.3mf"
    source.write_bytes(b"immutable authored source")
    sha = model_job.file_sha256(source)
    job = {
        "title": "Painted model", "downloaded_file": str(source),
        "selected_source": {"name": "source.3mf", "path": str(source)},
        "source_checkpoint": {
            "schema": "source_checkpoint_v1", "status": "available",
            "source_path": str(source), "source_sha256": sha,
            "request_scope": {"platform": "discord", "requester_id": "u1",
                              "channel_id": "c1", "printer_id": "p1"},
            "authored_plates": [{"id": "authored-plate-2", "name": "Upper shell"}],
        },
    }
    (folder / "JOB.json").write_text(json.dumps(job))
    found = projects.find_source_checkpoints(platform="discord", requester_id="u1",
                                             channel_id="c1", printer_id="p1", archive=tmp_path / "archive")
    assert len(found) == 1
    assert found[0]["checkpoint"]["authored_plates"][0]["id"] == "authored-plate-2"
    assert projects.find_source_checkpoints(platform="discord", requester_id="u2",
                                            channel_id="c1", printer_id="p1", archive=tmp_path / "archive") == []

    source.write_bytes(b"changed source")
    assert projects.find_source_checkpoints(platform="discord", requester_id="u1",
                                            channel_id="c1", printer_id="p1", archive=tmp_path / "archive") == []


def test_checkpoint_discovery_reads_all_jobs_but_hashes_only_selected_record(tmp_path, monkeypatch):
    from print_handy.core import model_job

    archive = tmp_path / "archive"
    scope = {"platform": "discord", "requester_id": "u1",
             "channel_id": "c1", "printer_id": "p1"}
    paths = []
    for index in range(4):
        folder = archive / f"job-{index}"
        folder.mkdir(parents=True)
        source = folder / "source.3mf"
        source.write_bytes(f"source {index}".encode())
        digest = model_job.file_sha256(source)
        job = {"downloaded_file": str(source), "source_checkpoint": {
            "schema": projects.SOURCE_CHECKPOINT_SCHEMA, "status": "available",
            "request_scope": scope, "source_path": str(source), "source_sha256": digest,
            "title": f"Project {index}", "authored_plates": [{"id": "1"}]}}
        job_path = folder / "JOB.json"
        job_path.write_text(json.dumps(job))
        paths.append((job_path, source, digest))

    reads = []
    original_read_text = Path.read_text
    def count_read(path, *args, **kwargs):
        if path.name == "JOB.json":
            reads.append(str(path))
        return original_read_text(path, *args, **kwargs)
    hashes = []
    original_hash = model_job.file_sha256
    def count_hash(path):
        hashes.append(str(path))
        return original_hash(path)
    monkeypatch.setattr(Path, "read_text", count_read)
    monkeypatch.setattr(model_job, "file_sha256", count_hash)

    # Discovery must omit internally inconsistent legacy job-level digests.
    inconsistent = json.loads(paths[3][0].read_text())
    inconsistent["source_sha256"] = "f" * 64
    paths[3][0].write_text(json.dumps(inconsistent))
    reads.clear()

    candidates = projects.discover_source_checkpoints(**scope, archive=archive)
    assert len(candidates) == 3
    assert all(row["job_json"] != str(paths[3][0]) for row in candidates)
    assert len(reads) == 4
    assert hashes == []
    selected = candidates[2]
    loaded = projects.load_source_checkpoint(selected["job_json"], **scope, archive=archive,
        expected_source=selected["checkpoint"]["source_path"],
        expected_sha256=selected["checkpoint"]["source_sha256"])
    assert loaded and loaded["checkpoint"]["authored_plates"] == [{"id": "1"}]
    assert len(reads) == 5
    assert hashes == [str(paths[2][1])]

    # Revalidation rejects both disappearance and a changed source, still hashing
    # only the one explicitly selected record per attempt.
    paths[2][1].unlink()
    assert projects.load_source_checkpoint(selected["job_json"], **scope, archive=archive) is None
    paths[2][1].write_bytes(b"changed")
    assert projects.load_source_checkpoint(selected["job_json"], **scope, archive=archive) is None
    assert hashes == [str(paths[2][1]), str(paths[2][1])]


    checkpoint = {
        'authored_parts': [{'key': 'a'}, {'key': 'b'}],
        'color_manifest': {'plate_usage': {'2': {'part_keys': ['a']}, '7': {'part_keys': ['b']}}},
    }
    assert projects.checkpoint_part_plate(checkpoint, '2') == '7'
    checkpoint['color_manifest']['plate_usage']['7']['part_keys'].append('a')
    assert projects.checkpoint_part_plate(checkpoint, '2') is None


def test_source_checkpoint_refuses_unbound_or_incomplete_provenance():
    with pytest.raises(ValueError, match="SHA256"):
        projects.source_checkpoint({"downloaded_file": "/archive/a.3mf"}, {"source_sha256": "bad"}, None)
    with pytest.raises(ValueError, match="durable source path"):
        projects.source_checkpoint({}, {"source_sha256": "a" * 64}, None)
    checkpoint = projects.source_checkpoint(
        {"downloaded_file": "/archive/a.3mf"}, {"source_sha256": "A" * 64, "parts": []},
        {"platform": "discord", "requester_id": "u1", "channel_id": "", "printer_id": "p1"})
    assert checkpoint["source_sha256"] == "a" * 64
    assert checkpoint["request_scope"] is None
