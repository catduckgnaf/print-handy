"""Per-Z analysis helpers for issue #721 (controlled per-height cooling / speed test).

Pure functions only: build the controlled STL fixtures and read Orca G-code back as
per-layer records, so the real-slicer tests can answer, from sliced output rather
than assertion:

- does layer-time cooling / slowdown engage on short top layers?
- does bridge / overhang fan engage where bridges occur high in the print?
- is outer-wall speed / acceleration constant with height?

Fan handling: Orca sets the part fan with M106 and may leave it unchanged for many
layers (the PLA templates run fan_min_speed == fan_max_speed == 100%), so the
analyzer tracks the active fan state across the whole file — including the header
before the first layer — and attributes that state to each layer and feature.
Outer-wall feeds count only extruding moves (a G1 with an E parameter); travel
moves under the same feature comment are not wall speed.
"""
import re

_BOX_FACES = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
              (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]


def _box_tris(x0, y0, z0, x1, y1, z1):
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    return [[v[i] for i in face] for face in _BOX_FACES]


def write_fixture(path, boxes):
    """ASCII STL from axis-aligned boxes [(x0, y0, z0, x1, y1, z1), ...]."""
    lines = ["solid fixture"]
    for box in boxes:
        for tri in _box_tris(*box):
            lines.append("facet normal 0 0 0")
            lines.append(" outer loop")
            lines += [f"  vertex {p[0]} {p[1]} {p[2]}" for p in tri]
            lines.append(" endloop")
            lines.append("endfacet")
    lines.append("endsolid fixture")
    path.write_text("\n".join(lines) + "\n")
    return path


# The four controlled fixtures from #721. Dimensions are model facts, not print settings.
FIXTURES = {
    # 10 x 10 mm cross-section, 140 mm tall: the tall-part shape from #715.
    "tall_thin": [(0, 0, 0, 10, 10, 140)],
    # Two pillars with a beam across the top: the beam underside at z=120 is a bridge high up.
    "tall_bridge_top": [(0, 0, 0, 8, 20, 120), (52, 0, 0, 60, 20, 120), (0, 0, 120, 60, 20, 128)],
    # Wide base, 8 x 8 mm post on top: top layers are far shorter than base layers.
    "small_top": [(0, 0, 0, 40, 40, 20), (16, 16, 20, 24, 24, 100)],
    # Short control: nothing about height can engage here.
    "short_control": [(0, 0, 0, 40, 40, 10)],
}

_FAN_RE = re.compile(r"^M106(?:\s+P(\d+))?(?:\s+S(\d+(?:\.\d+)?))?")
_TYPE_RE = re.compile(r"^;TYPE:(.+)$")
_MOVE_RE = re.compile(r"^G1\s+(.*)$")
_PARAM_RE = re.compile(r"([XYZEF])(-?\d+(?:\.\d+)?)")
_ACCEL_RE = re.compile(r"^M204\s+.*?[PS](\d+(?:\.\d+)?)")
_CONFIG_RE = re.compile(r"^;\s*([a-z_0-9]+)\s*=\s*(\S.*)$")


def analyze_gcode(text):
    """Split Orca G-code into per-layer records.

    Each record: {z, fan_states: [active part-fan values seen in the layer],
    types: set of feature names, outer_wall_feeds: [mm/min, extruding moves only],
    bridge_fan_max: active fan while a bridge feature was printing (or None when the
    layer has no bridge feature), accelerations: set from M204}.
    The part fan is M106 with no P index or P1; other indices are auxiliary/exhaust.
    """
    layers = []
    current = None
    feature = None
    fan_state = None

    def new_layer(z):
        return {"z": z, "fan_states": [], "types": set(), "outer_wall_feeds": [],
                "bridge_fan_max": None, "accelerations": set()}

    for raw in text.splitlines():
        line = raw.strip()
        m = _FAN_RE.match(line)
        if m and m.group(2) is not None and m.group(1) in (None, "1"):
            fan_state = float(m.group(2))
            if current is not None:
                current["fan_states"].append(fan_state)
            continue
        if line.startswith("M107"):
            fan_state = 0.0
            if current is not None:
                current["fan_states"].append(fan_state)
            continue
        if line == ";LAYER_CHANGE":
            current = new_layer(None)
            if fan_state is not None:
                current["fan_states"].append(fan_state)
            layers.append(current)
            feature = None
            continue
        if current is None:
            continue
        if line.startswith(";Z:"):
            try:
                current["z"] = float(line[3:])
            except ValueError:
                pass
            continue
        m = _TYPE_RE.match(line)
        if m:
            feature = m.group(1).strip()
            current["types"].add(feature)
            if "bridge" in feature.lower() and fan_state is not None:
                current["bridge_fan_max"] = max(fan_state, current["bridge_fan_max"] or 0.0)
            continue
        m = _ACCEL_RE.match(line)
        if m:
            current["accelerations"].add(float(m.group(1)))
            continue
        m = _MOVE_RE.match(line)
        if m:
            params = dict(_PARAM_RE.findall(m.group(1)))
            if current["z"] is None and "Z" in params:
                current["z"] = float(params["Z"])
            if feature and "outer" in feature.lower() and "E" in params and "F" in params:
                current["outer_wall_feeds"].append(float(params["F"]))
            if feature and "bridge" in feature.lower() and fan_state is not None:
                current["bridge_fan_max"] = max(fan_state, current["bridge_fan_max"] or 0.0)
    return [layer for layer in layers if layer["z"] is not None]


def footer_config(text):
    """The `; key = value` config block Orca appends to its G-code (single values:
    evidence of whether a setting is constant for the whole print)."""
    config = {}
    for raw in text.splitlines():
        m = _CONFIG_RE.match(raw.strip())
        if m:
            config[m.group(1)] = m.group(2).strip()
    return config


def band(layers, z_min, z_max):
    return [layer for layer in layers if z_min <= layer["z"] < z_max]


def fan_max(layers):
    values = [v for layer in layers for v in layer["fan_states"]]
    return max(values) if values else None


def outer_wall_feed_range(layers):
    values = [v for layer in layers for v in layer["outer_wall_feeds"]]
    return (min(values), max(values)) if values else (None, None)
