#!/usr/bin/env python3
"""Erase leftover OPAQUE background out of a sheet's cut-out holes.

Why this exists
---------------
Background removal floods in from the OUTSIDE of the drawing, so it only reaches what the
silhouette lets it reach. Any hole fully enclosed by the character - the gap under a raised
arm, between the legs, inside a curled tail - is never visited, and the original source
matte survives there as fully opaque pixels.

You cannot fix this by deleting a colour: eyes, highlights, teeth, clothing, and body details
can be indistinguishable from a small matte pocket. So there is no safe fully-automatic rule,
and this tool does not pretend otherwise: YOU mark the pockets, it does the pixel work.

What it does
------------
One mark = one seed pixel. The tool flood-fills from it over pixels within `--tolerance` of
the seed colour (max channel difference, 4-connected, stopping at anything already
transparent), and clears that region:

  * the region's alpha goes to 0;
  * a `--feather` band around it gets a PARTIAL alpha, scaled by how close each pixel still
    is to the seed colour, so the antialiased white fringe between the pocket and the black
    outline goes with it instead of surviving as a halo;
  * erased and feathered pixels have their RGB replaced by the nearest surviving colour
    (edge padding), so bilinear filtering and mipmaps cannot bleed white back in later.

Geometry is untouched - no pixel moves and frame rectangles are not rewritten. The tool checks
the drawn bounding box of every frame it edits and warns if one changed, which can happen only
when the pocket reaches the silhouette edge.

A pose barely moves inside a loop, so a pocket marked on one frame is often present nearby in
the rest. When you explicitly enable `--propagate`, it maps the seed into every other
frame through the .meta rects, looks for the same colour within `--search` px, and fills
there too - refusing any frame where the region comes out wildly BIGGER than the one you
marked, because that is what a fill leaking into the body or onto an eye looks like. Smaller
is fine and expected: a pocket can open, close, or shrink substantially across an animation.
Every frame is listed in the report, and a
before/after contact sheet - cropped to what actually changed - is written beside the source
PNG so you can look at what happened.

Not every pocket wants to become a hole. One that is fully ENCLOSED by the drawing turns into
a slit that shows the scene behind it; `--paint` fills it with the surrounding colour
instead. A gap that opens to the outside is the opposite case and wants the erase.

Marking by hand is what the companion UI is for:

    open sprite_pocket_cleaner.html in a browser, drop the .png (and its .png.meta) on it,
    click the leftover matte areas, then export plan.json

Usage (from the repo root):
    python3 sprite_pocket_cleaner.py --scan sprites                   # hints, writes nothing
    python3 sprite_pocket_cleaner.py --plan ~/Downloads/plan.json --dry-run
    python3 sprite_pocket_cleaner.py --plan ~/Downloads/plan.json
    python3 sprite_pocket_cleaner.py sprites/idle.png --at 812,431
    python3 sprite_pocket_cleaner.py sprites/idle.png --restore

`sheet` is a PNG path. The original PNG is copied to an `.art-backup/` directory beside it
before anything is written; use `--restore` to put that original back.

An INDEXED (mode "P") png STAYS indexed: only the pixels that actually changed are re-indexed
against the sheet's own palette (erased ones take its transparent entry, feathered ones the
nearest entry by colour and alpha), so a 917 KB delivered sheet does not come back as a 3 MB
RGBA one for the sake of a few hundred pixels. `--rgba` forces the conversion, and it happens
by itself if the palette has no transparent entry to erase into.

Two things this tool deliberately will not do. It will not guess which blobs are background
(see above - it cannot be done safely on this art; --scan only prints hints for you to look
at). And it will not fill from a seed that is dark or saturated, i.e. a misclick on the fur,
unless you pass --any-colour or explicitly mark that one reviewed pocket as a dark/coloured
matte in the companion page.
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
    from scipy import ndimage
except ImportError:
    raise SystemExit('needs Pillow + numpy + scipy: python3 -m pip install pillow numpy scipy')

def resolve_sheet(sheet: str) -> Path:
    """Resolve a PNG path, accepting an omitted `.png` suffix as a convenience."""
    given = Path(sheet).expanduser()
    candidates = [given]
    if given.suffix.lower() != '.png':
        candidates.append(given.with_suffix('.png'))
    for cand in candidates:
        if cand.suffix == '.png' and cand.is_file():
            return cand.resolve()
    tried = '\n  '.join(str(p) for p in candidates)
    raise SystemExit(f'no such sheet - tried:\n  {tried}')


def backup_path(png: Path) -> Path:
    """Keep the original beside its source PNG, never overwriting an older backup."""
    return png.parent / '.art-backup' / png.name


def show(path: Path) -> str:
    """Print paths relative to the current directory when possible."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(Path.cwd()))
    except ValueError:
        pass
    return str(resolved)

DEFAULT_TOLERANCE = 30
DEFAULT_FEATHER = 2
DEFAULT_SEARCH = 10
# A pocket is a hole in the drawing, not a limb: anything past this share of the frame's
# drawn pixels is a fill that leaked through a gap, not the thing you clicked on.
DEFAULT_MAX_AREA = 0.15
# How far a propagated region may differ from the marked one before it is refused.
AREA_RATIO = 4.0
# How many candidate seeds a propagated mark tries per frame before giving up.
SEED_CANDIDATES = 24
# "Background-like" seed: light and near-neutral. Guards a misclick on fur.
SEED_MIN_LIGHT = 190
SEED_MAX_SAT = 40

CROSS = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)   # 4-connectivity


def frame_rects(meta_path: Path):
    """[(name, x, y, w, h)] in frame order, y measured from the BOTTOM (Unity's convention)."""
    text = meta_path.read_text(errors='ignore')
    pairs = re.findall(
        r'name: ([^\n]+)\n\s*rect:\n\s*serializedVersion: 2\n'
        r'\s*x: ([\d.]+)\n\s*y: ([\d.]+)\n\s*width: ([\d.]+)\n\s*height: ([\d.]+)', text)
    if not pairs:
        pairs = re.findall(
            r'name: ([^\n]+)\n\s*rect:\s*\{x: ([\d.]+), y: ([\d.]+), '
            r'width: ([\d.]+), height: ([\d.]+)\}', text)

    def order(item):
        m = re.search(r'_(\d+)$', item[0])
        return int(m.group(1)) if m else 0

    return [(n, int(float(x)), int(float(y)), int(float(w)), int(float(h)))
            for n, x, y, w, h in sorted(pairs, key=order)]


def box_of(rect, sheet_height):
    """Unity rect -> (x0, y0, x1, y1) with y from the TOP, for slicing the pixel array."""
    _, x, y, w, h = rect
    top = sheet_height - (y + h)
    return (x, top, x + w, top + h)


def frame_of(boxes, x, y):
    """Index of the frame containing (x, y) in top-left pixel coords, or None."""
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        if x0 <= x < x1 and y0 <= y < y1:
            return i
    return None


def rect_within_frame(rect, box):
    """Whether a rectangle is wholly contained by one frame rectangle."""
    x, y, w, h = rect
    x0, y0, x1, y1 = box
    return w > 0 and h > 0 and x0 <= x and y0 <= y and x + w <= x1 and y + h <= y1


def region_at(arr, seed, tolerance, box=None):
    """Boolean mask of the flood-filled region around `seed`, or None if the seed is empty.

    Membership is "within `tolerance` of the seed colour on every channel, and not already
    transparent", 4-connected. Restricting the work to `box` (a frame) is not just a speed
    trick: it stops a fill from crawling out of one frame's cell into its neighbour through
    a shared row of background pixels.
    """
    x, y = seed
    if arr[y, x, 3] == 0:
        return None
    x0, y0, x1, y1 = box if box else (0, 0, arr.shape[1], arr.shape[0])
    view = arr[y0:y1, x0:x1]
    rgb = view[..., :3].astype(np.int16)
    ref = arr[y, x, :3].astype(np.int16)
    near = (np.abs(rgb - ref).max(axis=2) <= tolerance) & (view[..., 3] > 0)
    lab, _ = ndimage.label(near, structure=CROSS)
    tag = lab[y - y0, x - x0]
    if tag == 0:
        return None
    mask = np.zeros(arr.shape[:2], dtype=bool)
    mask[y0:y1, x0:x1] = lab == tag
    return mask


def rect_region(arr, rect, tolerance, seed_rgb):
    """Every pixel inside `rect` within tolerance of `seed_rgb` - the escape hatch for a
    pocket a flood fill will not hold (a fringe broken up by the outline, say)."""
    x, y, w, h = rect
    mask = np.zeros(arr.shape[:2], dtype=bool)
    view = arr[y:y + h, x:x + w]
    rgb = view[..., :3].astype(np.int16)
    ref = np.asarray(seed_rgb, dtype=np.int16)
    mask[y:y + h, x:x + w] = (np.abs(rgb - ref).max(axis=2) <= tolerance) & (view[..., 3] > 0)
    return mask


def erase(arr, mask, seed_rgb, tolerance, feather, paint=False):
    """Clear `mask` from the sheet, feathering the edge and padding the RGB behind it.

    Alpha does the hiding; the RGB rewrite is what stops a white halo reappearing when the
    sprite is drawn at any size other than 1:1 - a fully transparent pixel still contributes
    its colour to bilinear filtering and to every mipmap level.

    `paint` keeps the alpha and only repaints the colour. Not every matte pocket wants to
    become a hole: one that is fully ENCLOSED by the drawing turns into a slit that shows the
    scene behind it, and filling it with the
    surrounding colour is usually the better answer. An armpit gap that opens to the outside
    is the opposite case and wants the erase.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return
    pad = feather + 4
    y0, y1 = max(0, ys.min() - pad), min(arr.shape[0], ys.max() + 1 + pad)
    x0, x1 = max(0, xs.min() - pad), min(arr.shape[1], xs.max() + 1 + pad)
    sub = arr[y0:y1, x0:x1]
    sub_mask = mask[y0:y1, x0:x1]

    band = np.zeros_like(sub_mask)
    if feather > 0 and not paint:
        grown = ndimage.binary_dilation(sub_mask, CROSS, iterations=feather)
        band = grown & ~sub_mask & (sub[..., 3] > 0)

    keep = (sub[..., 3] > 0) & ~sub_mask & ~band
    if keep.any():
        # nearest surviving pixel for every erased/feathered one
        _, (iy, ix) = ndimage.distance_transform_edt(~keep, return_indices=True)
        near_rgb = sub[..., :3][iy, ix]
    else:
        near_rgb = sub[..., :3]

    if band.any():
        ref = np.asarray(seed_rgb, dtype=np.int16)
        dist = np.abs(sub[..., :3].astype(np.int16) - ref).max(axis=2)
        # 0 at the seed colour, 1 once a pixel is a full tolerance clear of it
        t = np.clip((dist - tolerance) / float(max(1, tolerance)), 0.0, 1.0)
        bt = t[band]
        sub[..., 3][band] = (sub[..., 3][band] * bt).astype(np.uint8)
        blend = (1.0 - bt)[:, None]
        sub[..., :3][band] = (sub[..., :3][band] * (1 - blend) + near_rgb[band] * blend
                              ).astype(np.uint8)

    sub[..., :3][sub_mask] = near_rgb[sub_mask]
    if not paint:
        sub[..., 3][sub_mask] = 0


def drawn_box(arr, box):
    """Bounding box of the frame's drawn pixels, or None - used to spot a rect-invalidating
    edit (the only case where the anchors must be regenerated)."""
    x0, y0, x1, y1 = box
    ys, xs = np.nonzero(arr[y0:y1, x0:x1, 3] > 40)
    if len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def seed_is_background_like(rgb):
    lo, hi = int(min(rgb)), int(max(rgb))
    return lo >= SEED_MIN_LIGHT and (hi - lo) <= SEED_MAX_SAT


def seed_is_allowed(rgb, allow_any_colour=False):
    """Keep the artwork guard by default, with a deliberate per-mark escape hatch.

    The page writes ``allowAnyColour`` only when the artist has manually inspected the
    preview of that mark. This permits dark gray or coloured source mattes without making
    every other mark in the plan capable of eating fur, clothes, or eyes.
    """
    return allow_any_colour or seed_is_background_like(rgb)


def find_seeds(arr, box, point, ref_rgb, tolerance, search):
    """Every pixel near `point` inside `box` matching `ref_rgb`, nearest first.

    A propagated mark cannot use the original coordinates verbatim: the drawing shifts a few
    pixels between frames, and one pixel off lands on the outline instead of the pocket. The
    nearest match is not good enough either - it can lock onto a single stray antialiased pixel
    a few px away and "erase" 1 px while the
    real pocket sat untouched beside it. So all the candidates come back and the caller floods
    from each, keeping the biggest region that still passes the leak cap.
    """
    px, py = point
    x0, y0, x1, y1 = box
    lo_x, hi_x = max(x0, px - search), min(x1, px + search + 1)
    lo_y, hi_y = max(y0, py - search), min(y1, py + search + 1)
    if lo_x >= hi_x or lo_y >= hi_y:
        return []
    view = arr[lo_y:hi_y, lo_x:hi_x]
    rgb = view[..., :3].astype(np.int16)
    ref = np.asarray(ref_rgb, dtype=np.int16)
    hit = (np.abs(rgb - ref).max(axis=2) <= tolerance) & (view[..., 3] > 0)
    ys, xs = np.nonzero(hit)
    if len(ys) == 0:
        return []
    gx, gy = xs + lo_x, ys + lo_y
    order = np.argsort((gx - px) ** 2 + (gy - py) ** 2)
    return [(int(gx[i]), int(gy[i])) for i in order]


def propagate(arr, boxes, frame, seed, mask, tolerance, search):
    """Find the same pocket in every OTHER frame: [(frame, mask or None, note)].

    This is the whole of the propagation rule, in one place, because
    sprite_pocket_cleaner.html reimplements it in JS to preview the result and the two must
    agree byte for byte - run --parity after touching either.
    """
    x, y = seed
    sx, sy = boxes[frame][0], boxes[frame][1]
    base = int(mask.sum())
    ref = brightest(arr, mask)
    out = []
    for j, box in enumerate(boxes):
        if j == frame:
            continue
        guess = (box[0] + (x - sx), box[1] + (y - sy))
        best, best_size, best_at, oversize = None, 0, None, 0
        for cand in find_seeds(arr, box, guess, ref, tolerance, search)[:SEED_CANDIDATES]:
            if best is not None and best[cand[1], cand[0]]:
                continue                                   # already inside the best region
            m2 = region_at(arr, cand, tolerance, box)
            if m2 is None:
                continue
            size = int(m2.sum())
            # Only the BIGGER direction is dangerous - that is what a fill leaking out of the
            # pocket looks like. Smaller is normal: a pocket can open and close over a loop,
            # and refusing the small end just leaves visible residue behind.
            if size > base * AREA_RATIO:
                oversize = max(oversize, size)
                continue
            if size > best_size:
                best, best_size, best_at = m2, size, cand
        if best is None:
            out.append((j, None, f'{oversize}px vs {base}px - too big, mark it by hand'
                        if oversize else 'no pocket there'))
        else:
            out.append((j, best, f'({best_at[0]},{best_at[1]})'))
    return out


def brightest(arr, mask):
    """The lightest colour in a region - a far more stable propagation reference than
    whatever pixel happened to be under the click, which is often a dim edge one."""
    px = arr[..., :3][mask].astype(np.int16)
    return px[np.argmax(px.sum(axis=1))].copy()


def load_sheet(png: Path):
    im = Image.open(png)
    was_indexed = im.mode == 'P'
    return np.array(im.convert('RGBA')), was_indexed


def repack_indexed(src: Image.Image, before, after):
    """Write the edit back into the ORIGINAL indexed image instead of returning RGBA.

    Several delivered sheets are mode "P" with a per-entry alpha table, and converting one to
    RGBA triples the file for the sake of a few hundred pixels. So only the pixels this tool
    actually changed are re-indexed - every other byte, and the palette itself, is the
    delivered one. Erased pixels take the palette's transparent entry; the handful of
    feathered ones take the nearest entry by colour AND alpha, which the palette can serve
    well because it already holds their neighbours.
    """
    pal = src.getpalette()
    if pal is None:
        return None
    entries = len(pal) // 3
    pal_rgb = np.asarray(pal[:entries * 3], dtype=np.int16).reshape(-1, 3)
    trans = src.info.get('transparency')
    pal_a = np.full(entries, 255, dtype=np.int16)
    if isinstance(trans, (bytes, bytearray)):
        pal_a[:len(trans)] = np.frombuffer(bytes(trans), dtype=np.uint8).astype(np.int16)
    elif isinstance(trans, int):
        pal_a[trans] = 0
    clear = np.nonzero(pal_a == 0)[0]
    if len(clear) == 0:
        return None                        # no transparent entry to erase into

    dirty = np.any(before != after, axis=2)
    if not dirty.any():
        return src
    idx = np.array(src)
    px = after[dirty].astype(np.int16)
    # alpha is weighted up: a wrong alpha shows as a hard edge, a wrong shade does not
    dist = (np.abs(px[:, None, :3] - pal_rgb[None]).sum(axis=2) +
            3 * np.abs(px[:, None, 3] - pal_a[None]))
    best = np.argmin(dist, axis=1).astype(idx.dtype)
    best[px[:, 3] == 0] = clear[0]
    idx[dirty] = best
    out = Image.fromarray(idx, 'P')
    out.putpalette(pal)
    if trans is not None:
        out.info['transparency'] = trans
    return out


def changed_crop(before, after, box, zoom=3.0, floor=48):
    """The window to show for one frame: what changed, plus context around it.

    Cropping the whole frame can hide a tiny edit in a contact-sheet tile. So the crop is driven
    by the edit and padded out so you can still see its surrounding artwork.
    """
    x0, y0, x1, y1 = box
    diff = np.any(before[y0:y1, x0:x1] != after[y0:y1, x0:x1], axis=2)
    ys, xs = np.nonzero(diff)
    if len(ys) == 0:
        return box
    w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    side = max(floor, int(max(w, h) * zoom))
    cx, cy = x0 + (xs.min() + xs.max()) // 2, y0 + (ys.min() + ys.max()) // 2
    half = side // 2
    return (max(x0, cx - half), max(y0, cy - half),
            min(x1, cx + half), min(y1, cy + half))


def contact_sheet(before, after, boxes, touched, dest: Path, limit=40):
    """before|after crops of every edited frame, on magenta so a stray hole is obvious."""
    if not touched:
        return None
    picks = sorted(touched)[:limit]
    tiles = []
    for i in picks:
        x0, y0, x1, y1 = changed_crop(before, after, boxes[i])
        for src in (before, after):
            crop = Image.fromarray(src[y0:y1, x0:x1], 'RGBA')
            bg = Image.new('RGBA', crop.size, (255, 0, 255, 255))
            bg.alpha_composite(crop)
            tiles.append(bg.convert('RGB'))
    tw = max(t.width for t in tiles)
    th = max(t.height for t in tiles)
    # scaled UP as well as down: these crops are often a few dozen pixels across, and a tile
    # you cannot see the edit in is not a review
    scale = 200.0 / max(tw, th)
    tw, th = max(1, int(tw * scale)), max(1, int(th * scale))
    cols = 8
    rows = (len(tiles) + cols - 1) // cols
    out = Image.new('RGB', (cols * tw, rows * th), (30, 30, 30))
    for k, tile in enumerate(tiles):
        out.paste(tile.resize((tw, th), Image.NEAREST), ((k % cols) * tw, (k // cols) * th))
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest)
    return dest


def cmd_scan(folder: str, min_area: int):
    """Rank light, enclosed, chunky blobs. HINTS ONLY - most of them will be eyes."""
    base = Path(folder).expanduser()
    if not base.is_dir():
        raise SystemExit(f'no such folder: {folder}')
    print('Light enclosed blobs, biggest first. These are CANDIDATES, not findings - eyes,')
    print('highlights and teeth look the same to this scan. Open the ones that surprise you.\n')
    print(f"{'blobs':>6} {'biggest':>8} {'at':>13}  sheet")
    print('-' * 96)
    rows = []
    for png in sorted(base.rglob('*.png')):
        arr, _ = load_sheet(png)
        rgb = arr[..., :3].astype(np.int16)
        alpha = arr[..., 3]
        body = alpha > 128
        light = ((rgb.min(axis=2) >= SEED_MIN_LIGHT) &
                 ((rgb.max(axis=2) - rgb.min(axis=2)) <= SEED_MAX_SAT) & body)
        if not light.any():
            continue
        lab, n = ndimage.label(light, CROSS)
        areas = np.bincount(lab.ravel())
        boxes = ndimage.find_objects(lab)
        found = []
        for i, sl in enumerate(boxes, start=1):
            if sl is None or areas[i] < min_area:
                continue
            ys, xs = sl
            h, w = ys.stop - ys.start, xs.stop - xs.start
            if areas[i] / float(h * w) < 0.45:      # crescent = an eye white, not a pocket
                continue
            found.append((int(areas[i]), int(xs.start), int(ys.start)))
        if found:
            found.sort(reverse=True)
            rows.append((found[0][0], len(found), found[0][1], found[0][2],
                         str(png.relative_to(base))[:-4]))
    rows.sort(reverse=True)
    for area, count, x, y, name in rows:
        print(f'{count:6d} {area:8d} {x:6d},{y:6d}  {name}')
    print('-' * 96)
    print(f'{len(rows)} sheet(s) with a light blob >= {min_area}px.')
    return 0


def cmd_restore(sheet: str):
    dest = resolve_sheet(sheet)
    src = backup_path(dest)
    if not src.exists():
        raise SystemExit(f'no backup at {show(src)}')
    shutil.copy2(src, dest)
    print(f'restored {show(dest)} from {show(src)}')
    return 0


def parse_marks(args):
    """Marks from --plan and/or --at/--rect, normalised to one shape.

    Also resolves --feather, which the UI records in the plan: an explicit flag wins, the
    plan's value is used when there is no flag, and the constant is the last word.
    """
    marks, sheet = [], args.sheet
    if args.plan:
        data = json.loads(Path(args.plan).read_text())
        sheet = sheet or data.get('sheet')
        defaults = data.get('defaults', {})
        if args.feather is None:
            args.feather = int(defaults.get('feather', DEFAULT_FEATHER))
        for raw in data.get('marks', []):
            mark = dict(defaults)
            mark.update(raw)
            marks.append(mark)
    for spec in args.at or []:
        x, y = (int(v) for v in spec.split(','))
        marks.append({'type': 'point', 'x': x, 'y': y})
    for spec in args.rect or []:
        x, y, w, h = (int(v) for v in spec.split(','))
        marks.append({'type': 'rect', 'x': x, 'y': y, 'w': w, 'h': h})
    if args.feather is None:
        args.feather = DEFAULT_FEATHER
    for mark in marks:
        mark.setdefault('type', 'point')
        mark.setdefault('tolerance', args.tolerance)
        mark.setdefault('propagate', bool(getattr(args, 'propagate', False)))
        if args.no_propagate:
            mark['propagate'] = False
    return sheet, marks


def _fake_sheet():
    """Two frames of a fake pet, each with an armpit POCKET and an EYE, both pure white.

    The pocket touches nothing; the eye is a white ring around a dark pupil, one pixel of
    white away from the pocket's colour. If a fill started at the pocket can reach the eye,
    or if propagation lands on the eye, this fixture catches it. No art assets involved.
    """
    h, w = 40, 40
    arr = np.zeros((h, 2 * w, 4), dtype=np.uint8)
    for f in range(2):
        cell = np.zeros((h, w, 4), dtype=np.uint8)
        shift = f * 6                               # the drawing moves between frames
        cell[6:38, 6:34, :3] = np.array([150, 110, 70])       # body
        cell[6:38, 6:34, 3] = 255
        cell[10 + shift:18 + shift, 10:16, :3] = 255          # eye white
        cell[10 + shift:18 + shift, 10:16, 3] = 255
        cell[12 + shift:16 + shift, 11:15, :3] = np.array([10, 10, 10])   # pupil
        cell[22 + shift:30 + shift, 24:30, :3] = 255          # the armpit pocket
        cell[22 + shift:30 + shift, 24:30, 3] = 255
        arr[:, f * w:(f + 1) * w] = cell
    boxes = [(f * w, 0, (f + 1) * w, h) for f in range(2)]
    return arr, boxes


def _parity_sheet():
    """Five frames whose pocket drifts and changes size, next to an eye it must never grab."""
    h = w = 48
    sizes = [(2, 4), (4, 9), (5, 12), (3, 7), (2, 4)]
    arr = np.zeros((h, len(sizes) * w, 4), dtype=np.uint8)
    for f, (pw, ph) in enumerate(sizes):
        drift = f * 2
        cell = np.zeros((h, w, 4), dtype=np.uint8)
        cell[6:44, 6:42, :3] = np.array([150, 110, 70])
        cell[6:44, 6:42, 3] = 255
        cell[10 + drift:18 + drift, 10:18, :3] = 255            # eye white
        cell[10 + drift:18 + drift, 10:18, 3] = 255
        cell[12 + drift:16 + drift, 12:16, :3] = np.array([10, 10, 10])
        cy, cx = 28 + drift, 30
        cell[cy - ph:cy + ph, cx - pw:cx + pw, :3] = 255        # the pocket
        cell[cy - ph:cy + ph, cx - pw:cx + pw, 3] = 255
        arr[:, f * w:(f + 1) * w] = cell
    boxes = [(f * w, 0, (f + 1) * w, h) for f in range(len(sizes))]
    return arr, boxes, (2 * w + 30, 28 + 2 * 2)                 # seed inside frame 2's pocket


def cmd_parity():
    """Prove the browser UI previews EXACTLY what this tool writes.

    They are two implementations of one algorithm, and they have already drifted apart once:
    a fix to the seed search landed here and not there, which would have made the page show a
    clean result while the png kept its pocket. So the JS is loaded out of the shipped html
    and run over the same pixels, and the two answers must match frame for frame.
    """
    import json
    import subprocess
    import tempfile

    script = Path(__file__).with_name('sprite_pocket_cleaner_parity.js')
    page = Path(__file__).with_name('sprite_pocket_cleaner.html')
    if not script.exists() or not page.exists():
        print(f'missing {script.name} or {page.name}')
        return 1

    arr, boxes, seed = _parity_sheet()
    frame = frame_of(boxes, *seed)
    mask = region_at(arr, seed, DEFAULT_TOLERANCE, boxes[frame])
    mine = [0] * len(boxes)
    mine[frame] = int(mask.sum())
    for j, m2, _ in propagate(arr, boxes, frame, seed, mask, DEFAULT_TOLERANCE, DEFAULT_SEARCH):
        mine[j] = int(m2.sum()) if m2 is not None else 0

    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / 'fixture'
        arr.tofile(str(fixture) + '.rgba')
        Path(str(fixture) + '.json').write_text(json.dumps({
            'w': int(arr.shape[1]), 'h': int(arr.shape[0]),
            'boxes': [list(b) for b in boxes], 'seed': list(seed), 'frame': frame,
            'tolerance': DEFAULT_TOLERANCE, 'search': DEFAULT_SEARCH,
            'areaRatio': AREA_RATIO, 'seedCandidates': SEED_CANDIDATES,
            'guardSeeds': [[255, 255, 255], [100, 100, 100], [150, 110, 70]]}))
        try:
            done = subprocess.run(['node', str(script), str(fixture)],
                                  capture_output=True, text=True)
        except FileNotFoundError:
            print('SKIPPED: node is not installed, so the browser half cannot be run here.')
            print('         Install node, or check the UI by hand against a --dry-run report.')
            return 0
    if done.returncode != 0:
        print(done.stdout + done.stderr)
        return 1
    theirs = json.loads(done.stdout)
    mine = {
        'per': mine,
        'guards': [seed_is_background_like(rgb)
                   for rgb in [(255, 255, 255), (100, 100, 100), (150, 110, 70)]],
    }

    print(f'  python (this tool) : {mine}')
    print(f'  js (sprite pocket cleaner) : {theirs}')
    if mine == theirs:
        print('\nPARITY OK - the page previews exactly what gets written.')
        return 0
    print('\nPARITY BROKEN - the page would preview something else than this tool writes.\n'
          'Both implement one algorithm: 4-connectivity, "max per-channel difference from the\n'
          'seed <= tolerance AND alpha > 0", never crossing a frame rect, propagation seeded\n'
          'from the region\'s BRIGHTEST colour with only an upper size guard. Fix the one that\n'
          'drifted.')
    return 1


def self_test():
    """Check the invariants this tool exists to hold. Touches no files."""
    checks, failed = [], 0

    def ok(name, cond):
        nonlocal failed
        checks.append((name, bool(cond)))
        if not cond:
            failed += 1

    arr, boxes = _fake_sheet()
    before = arr.copy()
    seed = (26, 24)                                  # inside frame 0's pocket
    mask = region_at(arr, seed, DEFAULT_TOLERANCE, boxes[0])
    ok('the pocket is found', mask is not None and mask.sum() == 48)
    ok('the fill does not reach the eye', not mask[10:18, 10:16].any())
    ok('the fill stays inside its own frame', not mask[:, 40:].any())

    erase(arr, mask, arr[seed[1], seed[0], :3].copy(), DEFAULT_TOLERANCE, DEFAULT_FEATHER)
    ok('the pocket is transparent afterwards', arr[24, 26, 3] == 0)
    ok('the eye is untouched', np.array_equal(before[10:18, 10:16], arr[10:18, 10:16]))
    ok('the erased pixels lost their white RGB (no halo left to bleed)',
       arr[22:30, 24:30, :3].max() < 200)
    ok('the other frame is byte-identical', np.array_equal(before[:, 40:], arr[:, 40:]))

    # Propagation: frame 1 is the same drawing one pixel down, so the seed must be re-found.
    local = (seed[0] - boxes[0][0], seed[1] - boxes[0][1])
    guess = (boxes[1][0] + local[0], boxes[1][1] + local[1])
    ok('the naive mapped point lands on the BODY, not the moved pocket',
       tuple(int(v) for v in before[guess[1], guess[0], :3]) == (150, 110, 70))
    cands = find_seeds(arr, boxes[1], guess, (255, 255, 255), DEFAULT_TOLERANCE, DEFAULT_SEARCH)
    ok('propagation re-finds the pocket in the next frame', len(cands) > 0)
    mask2 = region_at(arr, cands[0], DEFAULT_TOLERANCE, boxes[1])
    ok('the propagated region is the pocket, not the eye',
       mask2 is not None and mask2.sum() == 48 and not mask2[16:24, 50:56].any())

    # The propagation guard is UPPER-bound only: a pocket can shrink sharply across frames, and
    # a symmetric guard would silently skip that small end and leave visible residue behind.
    parr, pboxes, pseed = _parity_sheet()
    pmask = region_at(parr, pseed, DEFAULT_TOLERANCE, pboxes[2])
    sizes = [int(m.sum()) if m is not None else 0
             for _, m, _ in propagate(parr, pboxes, 2, pseed, pmask,
                                      DEFAULT_TOLERANCE, DEFAULT_SEARCH)]
    ok('a pocket that shrinks 7x is still propagated, not skipped',
       all(s > 0 for s in sizes) and min(sizes) * 7 < int(pmask.sum()))
    ok('propagation follows a pocket that drifts between frames', min(sizes) >= 30)
    # ...while the other direction still bites: mark the SMALL pocket (32px) and frame 2's
    # 240px one is 7.5x bigger, which is what a leak looks like, so it must be refused.
    small_seed = (30, 28)
    small = region_at(parr, small_seed, DEFAULT_TOLERANCE, pboxes[0])
    notes = {j: note for j, _, note in propagate(parr, pboxes, 0, small_seed, small,
                                                 DEFAULT_TOLERANCE, DEFAULT_SEARCH)}
    ok('a region several times bigger than the mark is refused as a leak',
       'too big' in notes.get(2, ''))

    # A seed on the fur must be refused rather than eating the body.
    ok('a fur seed is not background-like', not seed_is_background_like((150, 110, 70)))
    ok('a white seed is background-like', seed_is_background_like((255, 255, 255)))
    ok('a dark gray matte needs an explicit reviewed-mark override',
       not seed_is_allowed((100, 100, 100)) and seed_is_allowed((100, 100, 100), True))

    # The area guard: the body itself is 20x the pocket, so a fill that big must trip it.
    body = region_at(arr, (20, 34), 90, boxes[0])
    drawn = int((arr[boxes[0][1]:boxes[0][3], boxes[0][0]:boxes[0][2], 3] > 0).sum())
    ok('a leaked fill trips the area guard',
       body is not None and body.sum() > DEFAULT_MAX_AREA * drawn)

    for name, passed in checks:
        print(f'  {"PASS" if passed else "FAIL"}  {name}')
    print(f'\n{len(checks) - failed}/{len(checks)} checks passed')
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sheet', nargs='?', help='PNG sheet path (the .png suffix is optional)')
    ap.add_argument('--plan', help='plan.json exported by sprite_pocket_cleaner.html')
    ap.add_argument('--at', action='append', metavar='X,Y',
                    help='mark a pocket by seed pixel (sheet coordinates, top-left origin). '
                         'Repeatable.')
    ap.add_argument('--rect', action='append', metavar='X,Y,W,H',
                    help='erase every pixel within tolerance of the rect\'s brightest '
                         'background-like colour, inside the rect only. The escape hatch for '
                         'a pocket a flood fill will not hold. Repeatable.')
    ap.add_argument('--tolerance', type=int, default=DEFAULT_TOLERANCE,
                    help=f'max per-channel difference from the seed colour '
                         f'(default {DEFAULT_TOLERANCE})')
    ap.add_argument('--feather', type=int, default=None,
                    help=f'pixels of partial-alpha ramp around the region, which is what '
                         f'takes the antialiased white fringe with it (default '
                         f'{DEFAULT_FEATHER}, or whatever the plan recorded; 0 = hard edge)')
    ap.add_argument('--search', type=int, default=DEFAULT_SEARCH,
                    help=f'how far a propagated mark may hunt for the same pocket in another '
                         f'frame (default {DEFAULT_SEARCH}px)')
    ap.add_argument('--max-area', type=float, default=DEFAULT_MAX_AREA,
                    help=f'refuse a fill covering more than this share of the frame\'s drawn '
                         f'pixels (default {DEFAULT_MAX_AREA})')
    ap.add_argument('--propagate', action='store_true',
                    help='propagate direct CLI marks to nearby matching pockets in other frames')
    ap.add_argument('--no-propagate', action='store_true',
                    help='override a plan and edit only the frame each mark sits in')
    ap.add_argument('--paint', action='store_true',
                    help='fill the pocket with the surrounding colour instead of making it '
                         'transparent. Use it when the pocket is fully ENCLOSED by the drawing '
                         '- erasing one of those leaves a slit you can see the level through.')
    ap.add_argument('--rgba', action='store_true',
                    help='write RGBA even when the sheet was delivered INDEXED. By default an '
                         'indexed sheet stays indexed: only the changed pixels are re-indexed '
                         'against its own palette, so the file does not triple in size.')
    ap.add_argument('--any-colour', action='store_true',
                    help='allow a seed that is dark or saturated. Normally that is a misclick '
                         'on the drawing and is refused.')
    ap.add_argument('--dry-run', action='store_true', help='report only, write no png')
    ap.add_argument('--no-preview', action='store_true',
                    help='skip the before/after contact sheet')
    ap.add_argument('--no-backup', action='store_true',
                    help='skip the .art-backup/ copy (not recommended)')
    ap.add_argument('--restore', action='store_true',
                    help='copy the sheet back from .art-backup/ and exit')
    ap.add_argument('--scan', metavar='FOLDER',
                    help='list light enclosed blobs under a folder as marking '
                         'hints. Reads only, and most hits will be eyes.')
    ap.add_argument('--min-area', type=int, default=150, help='--scan blob floor (px)')
    ap.add_argument('--self-test', action='store_true',
                    help='check the tool against a synthetic sheet and exit')
    ap.add_argument('--parity', action='store_true',
                    help='check that sprite_pocket_cleaner.html previews exactly what this '
                         'tool writes, by running the page\'s own JS over the same pixels. '
                         'Run it after changing either. Needs node; skips cleanly without it.')
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.parity:
        return cmd_parity()
    if args.scan:
        return cmd_scan(args.scan, args.min_area)
    if args.restore:
        if not args.sheet:
            ap.error('--restore needs a sheet path')
        return cmd_restore(args.sheet)

    sheet, marks = parse_marks(args)
    if not sheet:
        ap.error('give a sheet path (or a --plan naming one)')
    if not marks:
        ap.error('nothing to erase - pass --plan, --at or --rect')

    png = resolve_sheet(sheet)
    meta = png.with_suffix('.png.meta')

    arr, was_indexed = load_sheet(png)
    before = arr.copy()
    rects = frame_rects(meta) if meta.exists() else []
    boxes = [box_of(r, arr.shape[0]) for r in rects]
    if not boxes:
        boxes = [(0, 0, arr.shape[1], arr.shape[0])]
        if not meta.exists():
            print('  note: no .png.meta beside the sheet - treating it as ONE frame, so '
                  'nothing propagates')
        else:
            print('  note: the .meta has no sliced sprites - treating the sheet as ONE frame')

    if was_indexed:
        fmt = ', indexed -> RGBA' if args.rgba else ', indexed (kept)'
    else:
        fmt = ''
    print(f'{sheet}  {len(boxes)} frame(s), {len(marks)} mark(s){fmt}\n')

    drawn = [int((arr[y0:y1, x0:x1, 3] > 0).sum()) for x0, y0, x1, y1 in boxes]
    touched, total_px, refused = set(), 0, 0

    for n, mark in enumerate(marks):
        if mark['type'] == 'rect':
            rect = (mark['x'], mark['y'], mark['w'], mark['h'])
            frame = frame_of(boxes, mark['x'], mark['y'])
            if frame is None or not rect_within_frame(rect, boxes[frame]):
                print(f'  mark {n}: rect {rect} crosses a frame boundary or lies outside a '
                      f'frame - skipped')
                refused += 1
                continue
            sub = arr[rect[1]:rect[1] + rect[3], rect[0]:rect[0] + rect[2]]
            body = sub[..., 3] > 0
            if not body.any():
                print(f'  mark {n}: rect {rect} holds nothing - skipped')
                refused += 1
                continue
            lum = sub[..., :3].astype(np.int16).sum(axis=2)
            iy, ix = np.unravel_index(np.argmax(np.where(body, lum, -1)), lum.shape)
            seed_rgb = sub[iy, ix, :3].copy()
            allow_any_colour = args.any_colour or bool(mark.get('allowAnyColour', False))
            if not seed_is_allowed(seed_rgb, allow_any_colour):
                print(f'  mark {n}: rect {rect} has no background-like colour in it '
                      f'(brightest is RGB {tuple(int(v) for v in seed_rgb)}) - skipped. '
                      f'Enable "allow dark / coloured matte" for this mark, or pass '
                      f'--any-colour to force every mark.')
                refused += 1
                continue
            mask = rect_region(arr, rect, mark['tolerance'], seed_rgb)
            plan = [(frame if frame is not None else 0, mask, seed_rgb, 'rect')]
        else:
            x, y = mark['x'], mark['y']
            if not (0 <= x < arr.shape[1] and 0 <= y < arr.shape[0]):
                print(f'  mark {n}: ({x},{y}) is outside the sheet - skipped')
                refused += 1
                continue
            seed_rgb = arr[y, x, :3].copy()
            if arr[y, x, 3] == 0:
                print(f'  mark {n}: ({x},{y}) is already transparent - skipped')
                refused += 1
                continue
            allow_any_colour = args.any_colour or bool(mark.get('allowAnyColour', False))
            if not seed_is_allowed(seed_rgb, allow_any_colour):
                print(f'  mark {n}: ({x},{y}) is RGB {tuple(int(v) for v in seed_rgb)}, '
                      f'which is not background-like - skipped. That is usually a misclick '
                      f'on the drawing; enable "allow dark / coloured matte" for this mark, '
                      f'or pass --any-colour to force every mark.')
                refused += 1
                continue
            frame = frame_of(boxes, x, y)
            if frame is None:
                print(f'  mark {n}: ({x},{y}) is not inside any frame rect - skipped')
                refused += 1
                continue
            mask = region_at(arr, (x, y), mark['tolerance'], boxes[frame])
            if mask is None:
                print(f'  mark {n}: nothing to fill at ({x},{y}) - skipped')
                refused += 1
                continue
            plan = [(frame, mask, seed_rgb, f'({x},{y})')]

            if mark['propagate'] and len(boxes) > 1:
                for j, m2, note in propagate(arr, boxes, frame, (x, y), mask,
                                             mark['tolerance'], args.search):
                    plan.append((j, m2, seed_rgb, note))

        print(f'  mark {n}: seed RGB {tuple(int(v) for v in plan[0][2])}, '
              f'tolerance {mark["tolerance"]}')
        for frame_index, mask, seed_rgb, note in plan:
            if mask is None:
                print(f'     fr {frame_index:2d} | skipped   | {note}')
                continue
            size = int(mask.sum())
            cap = args.max_area * max(1, drawn[frame_index])
            if size > cap:
                print(f'     fr {frame_index:2d} | REFUSED   | {size}px is '
                      f'{size / max(1, drawn[frame_index]):.0%} of the drawn frame - the fill '
                      f'leaked. Lower --tolerance, or use --rect.')
                refused += 1
                continue
            # always erased in memory, even on a dry run - that is what makes the preview
            # contact sheet show the actual result instead of two identical crops
            erase(arr, mask, seed_rgb, mark['tolerance'], args.feather, args.paint)
            touched.add(frame_index)
            total_px += size
            # a pocket is a few percent of the frame at most; anything bigger is under the
            # refusal cap but still worth a second look in the preview
            share = size / max(1, drawn[frame_index])
            big = f'  <<< {share:.0%} of the drawn frame' if share > 0.03 else ''
            print(f'     fr {frame_index:2d} | {size:6d}px | {note}{big}')
        print()

    if not touched:
        print('nothing erased.')
        return 1 if refused else 0

    moved = [i for i in sorted(touched)
             if drawn_box(before, boxes[i]) != drawn_box(arr, boxes[i])]
    if moved:
        print(f'  WARNING: the drawn bounding box changed on frame(s) '
              f'{", ".join(str(m) for m in moved)} - the pocket reached the silhouette edge.'
              f'\n           Update any dependent sprite anchors or metadata before shipping.\n')

    if not args.no_preview:
        dest = contact_sheet(before, arr, boxes, touched,
                             png.parent / '.sprite-pocket-cleaner-preview' / (png.stem + '.png'))
        if dest:
            print(f'  preview (before|after) -> {show(dest)}')

    if args.dry_run:
        print(f'\n  dry run - nothing written ({len(touched)} frame(s), {total_px}px would '
              f'be erased)')
        return 0

    if not args.no_backup:
        dest = backup_path(png)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():              # never overwrite an older, more original backup
            shutil.copy2(png, dest)
            print(f'  backup -> {show(dest)}')
        else:
            print(f'  backup already exists, kept -> {show(dest)}')
            # Keeping the older backup is right - it is the more original file - but it means
            # --restore is NOT an undo for THIS run. If another tool (deflicker, stabilize,
            # blink) has edited the sheet since that backup was taken, --restore throws those
            # away too, so say so with a number rather than let it be discovered later.
            stale = int(np.any(np.array(Image.open(dest).convert('RGBA')) != before,
                               axis=2).sum())
            if stale:
                print(f'  NOTE: that backup differs from the sheet you just edited by '
                      f'{stale} px,\n        so it predates other edits - `--restore` would '
                      f'undo those too, not just\n        this run. Use git to revert only '
                      f'this change.')

    out = None
    if was_indexed and not args.rgba:
        out = repack_indexed(Image.open(png), before, arr)
        if out is None:
            print('  note: the palette has no transparent entry - writing RGBA instead')
    if out is None:
        out = Image.fromarray(arr, 'RGBA')
    # Encode beside the destination, then atomically replace the source only after Pillow
    # finishes successfully. The backup remains available if encoding or replacement fails.
    with tempfile.NamedTemporaryFile(dir=png.parent, suffix='.png', delete=False) as handle:
        temp_png = Path(handle.name)
    try:
        if out.mode == 'P' and 'transparency' in out.info:
            out.save(temp_png, transparency=out.info['transparency'])
        else:
            out.save(temp_png)
        temp_png.replace(png)
    finally:
        if temp_png.exists():
            temp_png.unlink()
    print(f'  wrote {show(png)}  ({len(touched)} frame(s), {total_px}px erased'
          f'{", kept indexed" if out.mode == "P" else ""})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
