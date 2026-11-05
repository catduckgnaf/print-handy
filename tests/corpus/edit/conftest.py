"""Sharding configuration for the dedicated Print Handy edit corpus.

CI splits the edit corpus across parallel matrix shards via PRINT_HANDY_EDIT_SHARD="<index>/<count>".
Tests marked with `all_shards` (such as the Pixel Patina V12 multi-shard assertions) run in every shard.
Other tests are partitioned round-robin to preserve balanced runtime without lengthening CI.
Unset (local development) collects and runs everything.
"""
import os
from pathlib import Path
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "all_shards: mark test to run in all edit corpus shards")
    config.addinivalue_line("markers", "shard(n): pin test to a specific edit corpus shard index")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    spec = os.environ.get("PRINT_HANDY_EDIT_SHARD", "").strip()
    if not spec:
        return
    try:
        index_str, count_str = spec.split("/")
        index, count = int(index_str), int(count_str)
    except Exception as exc:
        raise pytest.UsageError(f"PRINT_HANDY_EDIT_SHARD must be '<index>/<count>', got {spec!r}: {exc}")
    if not (0 <= index < count):
        raise pytest.UsageError(f"PRINT_HANDY_EDIT_SHARD={spec!r}: index must be in 0..{count - 1}")

    keep = []
    drop = []
    regular_idx = 0
    for item in items:
        # Pinned or universal tests
        if item.get_closest_marker("all_shards"):
            keep.append(item)
            continue
        shard_marker = item.get_closest_marker("shard")
        if shard_marker:
            assigned_shard = int(shard_marker.args[0])
            if assigned_shard == index:
                keep.append(item)
            else:
                drop.append(item)
            continue

        # Regular tests dealt round-robin across shards
        if regular_idx % count == index:
            keep.append(item)
        else:
            drop.append(item)
        regular_idx += 1

    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
