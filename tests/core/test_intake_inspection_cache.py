import hashlib

from print_handy.core import intake_inspection_cache as cache


def test_two_plate_painted_inspection_reuses_pure_audit(tmp_path):
    source = b"canonical painted two-plate 3MF bytes"
    digest = hashlib.sha256(source).hexdigest()
    painted_two_plate_audit = {
        "sha256": digest,
        "color_manifest": {"painted": True, "plate_usage": {"1": {}, "2": {}},
                           "source_sha256": digest},
        "plates": 2,
    }
    calls = []

    def inspect():
        calls.append(1)
        return painted_two_plate_audit

    def cached_inspect():
        found = cache.get(tmp_path, "audit", digest, scope)
        if found is not None:
            return found
        result = inspect()
        cache.put(tmp_path, "audit", digest, scope, result)
        return result

    scope = {"platform": "discord", "requester_id": "requester", "channel_id": "channel",
             "printer_id": "printer"}
    assert cached_inspect() == painted_two_plate_audit
    assert cached_inspect() == painted_two_plate_audit
    assert len(calls) == 1


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
