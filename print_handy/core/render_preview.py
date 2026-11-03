"""Headless, validated STL preview rendering for Discord intake."""
from __future__ import annotations
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import settings

# The 2x2 multi-view sheet (#841): name -> OpenSCAD viewport rotation
# (the same rotations the OpenSCAD view menu uses for those views).
SHEET_VIEWS = (('ISO', (55, 0, 25)), ('FRONT', (90, 0, 0)),
               ('TOP', (0, 0, 0)), ('RIGHT', (90, 0, 90)))
SHEET_TILE = (450, 350)


def _openscad_cmd():
    """The OpenSCAD command prefix: xvfb-run only when present and headless.

    Both binaries are resolved on PATH (#841): a hard-coded /usr/bin path
    silently lost every preview on hosts whose OpenSCAD lives elsewhere
    (Homebrew, snap, AppImage, /usr/local).
    """
    openscad = shutil.which('openscad')
    if not openscad:
        raise RuntimeError('openscad not on PATH')
    xvfb = shutil.which('xvfb-run')
    prefix = [xvfb, '-a'] if xvfb and not os.environ.get('DISPLAY') else []
    return prefix + [openscad]


def _scene(path, scale, rotation, bed):
    """The preview .scad text plus camera facts for one STL.

    Returns (scad_text, (cam_x, cam_y, cam_z), distance, size): the model
    centered on the bed and resting on Z0, like a slicer would place it.
    """
    source = Path(path).resolve()
    if not source.is_file() or source.suffix.lower() != '.stl':
        raise ValueError('Preview is supported for STL models only.')
    matrix = rotation or [1, 0, 0, 0, 1, 0, 0, 0, 1]
    if len(matrix) != 9 or not all(math.isfinite(float(x)) for x in matrix):
        raise ValueError('The selected orientation is invalid.')
    s = float(scale)
    if not math.isfinite(s) or s <= 0:
        raise ValueError('The selected scale is invalid.')
    bedx, bedy = map(float, bed or settings.current()['printer']['bed_mm'])
    import numpy as np
    import trimesh
    mesh = trimesh.load(str(source), force='mesh')
    lin = np.array(matrix, dtype=float).reshape(3, 3) * s
    pts = np.asarray(mesh.vertices) @ lin.T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    off = [bedx / 2 - (lo[0] + hi[0]) / 2, bedy / 2 - (lo[1] + hi[1]) / 2, -lo[2]]
    size = hi - lo
    # Center on the build plate and normalize negative/far-away authored origins.
    # The translation above is applied after the model's scale and rotation.
    fits = size[0] <= bedx + 1e-6 and size[1] <= bedy + 1e-6
    rgb = '[0.2,0.55,0.9]' if fits else '[0.9,0.25,0.2]'
    m = [[float(v) for v in row] for row in lin]
    scad = f'''$fn=24;
color([0.18,0.22,0.27]) translate([0,0,-0.7]) cube([{bedx},{bedy},0.7]);
color({rgb}) multmatrix([[{m[0][0]},{m[0][1]},{m[0][2]},{off[0]}],[{m[1][0]},{m[1][1]},{m[1][2]},{off[1]}],[{m[2][0]},{m[2][1]},{m[2][2]},{off[2]}],[0,0,0,1]]) import("{source}");
'''
    center = (bedx / 2, bedy / 2, size[2] / 2)
    distance = round(max(bedx, bedy, size[0], size[1]) * 2.4)
    return scad, center, distance, size


def _cache_dest(source, scale, rotation):
    dest = Path.home() / '.hermes/cache/model-previews'
    dest.mkdir(parents=True, exist_ok=True)
    key = abs(hash((str(source), scale, tuple(rotation or ())))) & 0xffffffff
    return dest / f'{Path(source).stem}-{key:08x}.png'


def _reject_blank(image):
    from PIL import ImageStat
    pixels = list(image.getdata())
    if not pixels:
        raise RuntimeError('The 3D preview was empty; no image was sent.')
    if max(ImageStat.Stat(image).stddev) < 2.0 or len(set(pixels[::max(1, len(pixels)//20000)])) < 4:
        raise RuntimeError('The 3D preview was blank or near-uniform; no image was sent.')


def _label(image, xy, text):
    """White text on a dark strip, drawn at xy (the design sheet labels)."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(image, 'RGBA')
    box = draw.textbbox((0, 0), text)
    w, h = box[2] - box[0], box[3] - box[1]
    x, y = xy
    draw.rectangle([x, y, x + w + 10, y + h + 8], fill=(0, 0, 0, 170))
    draw.text((x + 5, y + 4), text, fill=(255, 255, 255, 255))


def render(path, scale=1.0, rotation=None, bed=None):
    """Render an STL through OpenSCAD in a headless subprocess; reject blank output."""
    from PIL import Image
    source = Path(path).resolve()
    scad_text, (cx, cy, cz), distance, _size = _scene(path, scale, rotation, bed)
    cam = f'{cx},{cy},{cz},55,0,25,{distance}'
    with tempfile.TemporaryDirectory(prefix='model-preview-') as td:
        scad, output = Path(td) / 'preview.scad', Path(td) / 'preview.png'
        scad.write_text(scad_text)
        proc = subprocess.run(_openscad_cmd() + ['-o', str(output), '--camera=' + cam,
                                                 '--imgsize=900,700',
                                                 '--colorscheme=Tomorrow', str(scad)],
                              capture_output=True, text=True, timeout=90)
        if proc.returncode or not output.is_file():
            raise RuntimeError('The 3D preview renderer could not produce an image: ' + proc.stderr[-300:])
        image = Image.open(output).convert('RGB')
        _reject_blank(image)
        final = _cache_dest(source, scale, rotation)
        image.save(final)
        return str(final)


def render_sheet(path, scale=1.0, rotation=None, bed=None, dimensions=None, dest=None):
    """Render a 2x2 multi-view sheet (iso, front, top, right) of an STL (#841).

    Four orthographic OpenSCAD renders tiled with Pillow, each tile labeled
    with its view and the sheet labeled with the bounding-box dimensions, so
    a feature on the back or underside is visible and the scale is on the
    image itself. Raises RuntimeError when no view can be produced.
    """
    from PIL import Image
    source = Path(path).resolve()
    scad_text, (cx, cy, cz), distance, _size = _scene(path, scale, rotation, bed)
    tile_w, tile_h = SHEET_TILE
    cmd_base = _openscad_cmd()
    with tempfile.TemporaryDirectory(prefix='model-preview-') as td:
        scad = Path(td) / 'preview.scad'
        scad.write_text(scad_text)
        sheet = Image.new('RGB', (tile_w * 2, tile_h * 2), (24, 28, 34))
        for index, (name, (rx, ry, rz)) in enumerate(SHEET_VIEWS):
            output = Path(td) / f'view-{index}.png'
            cam = f'{cx},{cy},{cz},{rx},{ry},{rz},{distance}'
            proc = subprocess.run(cmd_base + ['-o', str(output), '--camera=' + cam,
                                              '--projection=o',
                                              f'--imgsize={tile_w},{tile_h}',
                                              '--colorscheme=Tomorrow', str(scad)],
                                  capture_output=True, text=True, timeout=90)
            if proc.returncode or not output.is_file():
                raise RuntimeError('The 3D preview renderer could not produce an image: '
                                   + proc.stderr[-300:])
            tile = Image.open(output).convert('RGB')
            xy = ((index % 2) * tile_w, (index // 2) * tile_h)
            sheet.paste(tile, xy)
            _label(sheet, (xy[0] + 6, xy[1] + 6), name)
    if dimensions:
        dims = ' × '.join(f'{float(v):g}' for v in dimensions)
        _label(sheet, (6, sheet.height - 24), f'{dims} mm')
    _reject_blank(sheet)
    final = Path(dest) if dest else _cache_dest(source, scale, rotation)
    sheet.save(final)
    return str(final)
