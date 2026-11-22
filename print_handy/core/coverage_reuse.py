"""Exact artifact and coverage-input identities for preview coverage reuse."""
import hashlib
import json


def coverage_identity(gcodes, regions, inputs, plates):
    """Return an identity only for readable, byte-hashed G-code and explicit inputs."""
    try:
        hashes = []
        for path in gcodes:
            digest = hashlib.sha256()
            with open(path, 'rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            hashes.append(digest.hexdigest())
        mappings = [{k: v for k, v in plate.items() if k != 'gcode'} for plate in plates]
        if not hashes or len(hashes) != len(mappings) or any(
                not plate.get('source_bounds') or plate.get('unverified_reason') for plate in mappings):
            return None
        payload = {'gcode_sha256': hashes, 'regions': regions, 'inputs': inputs, 'plates': mappings}
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()
    except (OSError, TypeError, ValueError):
        return None


def coverage_plates(gcodes, job, extent=None, bounds_note=None):
    """Build the same per-plate mapping records used by the start-time coverage check."""
    fit = job.get('fit') or {}
    recorded = job.get('scale') or fit.get('scale')
    placement = fit.get('placement_mm')
    bounds = extent.get('bounds') if isinstance(extent, dict) else None
    height = extent.get('height') if isinstance(extent, dict) else None
    part_count = extent.get('part_count') if isinstance(extent, dict) else None
    plates = []
    for gcode in gcodes:
        plate = {'gcode': str(gcode), 'source_bounds': tuple(bounds) if bounds else None,
                 'source_height': height}
        if part_count:
            plate['source_part_count'] = part_count
        if recorded:
            plate['recorded_scale'] = float(recorded)
        if placement:
            plate['placement_mm'] = tuple(float(v) for v in placement)
        if bounds_note:
            plate['unverified_reason'] = bounds_note
        plates.append(plate)
    return plates


def reusable(result):
    return (isinstance(result, dict) and result.get('checked') is True
            and isinstance(result.get('ok'), bool) and not result.get('unverified'))


def check_for_preview(gcodes, regions, inputs, plates):
    from .support_clearance import verify_coverage_by_plate

    identity = coverage_identity(gcodes, regions, inputs, plates)
    result = verify_coverage_by_plate(regions, plates)
    if (not reusable(result) or not identity
            or coverage_identity(gcodes, regions, inputs, plates) != identity):
        identity = None
    return result, identity


def coverage_inputs(job):
    """The shared coverage inputs; geometry mapping, placement and printer identity matter."""
    fit = job.get('fit') or {}
    return {
        'printer_id': str(job.get('printer_id') or ''),
        'printer': job.get('printer'),
        'plate_mode': job.get('plate_mode'),
        'selected_plates': job.get('selected_plates'),
        'derived_plates': job.get('derived_plates'),
        'tool_mapping': job.get('tool_mapping'),
        'fit': fit,
        'placement_mm': fit.get('placement_mm'),
        'scale': job.get('scale') or fit.get('scale'),
    }
