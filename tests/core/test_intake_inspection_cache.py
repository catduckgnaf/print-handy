import hashlib
import json

from print_handy.core import intake_inspection_cache as cache


def test_audit_cache_strips_request_path_and_rebinds_fresh_and_cached_results(tmp_path):
    digest = hashlib.sha256(b"canonical painted two-plate 3MF bytes").hexdigest()
    scope = {"platform": "discord", "requester_id": "requester", "channel_id": "channel",
             "printer_id": "printer"}
    first_path = tmp_path / "request-one" / "source.3mf"
    second_path = tmp_path / "request-two" / "source.3mf"
    audit = {
        "path": first_path.name,
        "sha256": digest,
        "color_manifest": {"painted": True, "plate_usage": {"1": {}, "2": {}},
                           "source_sha256": digest},
        "plates": 2,
    }

    cache.put(tmp_path, "audit", digest, scope, audit)
    cache_path = cache._path(tmp_path, "audit", digest, scope)
    disk_entry = json.loads(cache_path.read_text())
    assert "path" not in disk_entry["payload"]

    # Both freshly computed and cached results are associated with this request's path.
    fresh_result = cache.bind_audit_to_path(audit, second_path)
    cached_result = cache.bind_audit_to_path(cache.get(tmp_path, "audit", digest, scope), second_path)
    assert cached_result == fresh_result
    assert cached_result["path"] == str(second_path)
    assert cached_result["color_manifest"] == audit["color_manifest"]


def test_inspection_cache_misses_for_source_settings_scope_and_corruption(tmp_path):
    digest = hashlib.sha256(b"source one").hexdigest()
    scope = {"requester_id": "one", "channel_id": "a", "printer_id": "p"}
    calls = []

    def inspect():
        calls.append(1)
        return {"finding": len(calls)}

    def run(digest, scope, inputs):
        found = cache.get(tmp_path, "clearance", digest, scope, inputs=inputs)
        if found is not None:
            return found
        result = inspect()
        cache.put(tmp_path, "clearance", digest, scope, result, inputs=inputs)
        return result

    first = run(digest, scope, {"first_layer_mm": 0.2})
    assert run(digest, scope, {"first_layer_mm": 0.2}) == first
    assert run(hashlib.sha256(b"source two").hexdigest(), scope,
               {"first_layer_mm": 0.2})["finding"] == 2
    assert run(digest, scope, {"first_layer_mm": 0.25})["finding"] == 3
    assert run(digest, {**scope, "channel_id": "b"},
               {"first_layer_mm": 0.2})["finding"] == 4
    path = cache._path(tmp_path, "clearance", digest, scope, {"first_layer_mm": 0.2})
    path.write_text('{broken')
    assert run(digest, scope, {"first_layer_mm": 0.2})["finding"] == 5
    assert len(calls) == 5
