#!/usr/bin/env python3
"""Generate the bundled sample sheet: a sprite sheet that HAS the defect this tool fixes.

No external art is bundled with this tool, so the sample is drawn from scratch here. It is not
decorative - it reproduces the four properties that make the defect hard:

  1. the character stands with its arms ARCHED onto its belt, so each arm closes a loop against
     the torso. The area inside that loop is a hole ENCLOSED by the drawing - the shape
     background removal always misses, because it can only flood in from outside the silhouette;
  2. on some frames those holes are left correctly TRANSPARENT - which is the evidence that the
     white on the other frames is leftover background rather than drawn art, and is exactly how
     the real sheets looked;
  3. white EYES that must survive, because "delete the white pixels" is the obvious wrong fix
     and this is what breaks it;
  4. per-frame DRIFT, so a mark cannot simply be copied to the same coordinates on every frame -
     propagation has to re-find the pocket.

The defect is not painted by hand. The frame is drawn, then its enclosed holes are found the
same way the real failure happens - a transparent region that the outside background cannot
reach - and filled with opaque white on the dirty frames. So the sample is the genuine article,
not an impression of it.

Run:  python3 sample/make_sample.py
Writes sample_sheet.png + sample_sheet.png.meta next to this script.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

HERE = Path(__file__).resolve().parent
FRAMES = 8
W, H = 96, 120
SS = 4                                    # supersampling, so edges are antialiased like real art
OUTLINE = (24, 20, 28, 255)
FUR = (150, 176, 214, 255)
BELLY = (196, 212, 236, 255)
BELT = (92, 104, 130, 255)
WHITE = (255, 255, 255, 255)
PUPIL = (30, 34, 46, 255)
# Frames where background removal failed and left the enclosed holes opaque white. The others
# keep them transparent, which is what the correct result looks like.
DIRTY = {2, 3, 4, 5}


def draw_figure(d, bob):
    """The character, at SS scale. Arms arch from the shoulder down onto the belt, so each one
    closes a loop against the torso - those two loops are the enclosed holes."""
    def s(*v):
        return tuple(int(round(n * SS)) for n in v)

    y = 8 + bob
    top = y + 48

    # head, ears
    d.ellipse(s(18, y, 78, y + 50), fill=FUR, outline=OUTLINE, width=3 * SS)
    d.polygon(s(24, y + 12, 32, y - 6, 42, y + 8), fill=FUR, outline=OUTLINE)
    d.polygon(s(72, y + 12, 64, y - 6, 54, y + 8), fill=FUR, outline=OUTLINE)

    # eyes - white, and they must survive
    for ex in (36, 60):
        d.ellipse(s(ex - 9, y + 18, ex + 9, y + 36), fill=WHITE, outline=OUTLINE, width=2 * SS)
        d.ellipse(s(ex - 4, y + 24, ex + 4, y + 33), fill=PUPIL)
        d.ellipse(s(ex - 3, y + 23, ex - 1, y + 26), fill=WHITE)     # highlight: also white

    # torso + belt
    d.rounded_rectangle(s(32, top, 64, top + 44), radius=int(10 * SS),
                        fill=FUR, outline=OUTLINE, width=3 * SS)
    d.ellipse(s(38, top + 8, 58, top + 32), fill=BELLY)
    d.rectangle(s(32, top + 26, 64, top + 32), fill=BELT, outline=OUTLINE, width=SS)

    # ARMS: a thick arc bulging away from the body, both ends landing on the torso, so the arm
    # and the torso together enclose a hole. Drawn dark then lighter on top = a fur band with an
    # outline down both sides.
    for box, start, end in (((18, top + 2, 48, top + 32), 90, 270),      # left arm, bulges left
                            ((48, top + 2, 78, top + 32), 270, 90)):     # right arm, bulges right
        d.arc(s(*box), start, end, fill=OUTLINE, width=int(7 * SS))
        d.arc(s(*box), start, end, fill=FUR, width=int(4 * SS))

    # legs
    d.rounded_rectangle(s(36, top + 40, 46, top + 54), radius=int(4 * SS),
                        fill=FUR, outline=OUTLINE, width=2 * SS)
    d.rounded_rectangle(s(50, top + 40, 60, top + 54), radius=int(4 * SS),
                        fill=FUR, outline=OUTLINE, width=2 * SS)


def enclosed_holes(alpha):
    """Transparent regions the OUTSIDE cannot reach - exactly what background removal misses."""
    labels, count = ndimage.label(alpha == 0)
    touching = set(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]).tolist())
    inner = [i for i in range(1, count + 1) if i not in touching]
    return np.isin(labels, inner) if inner else np.zeros(labels.shape, bool)


def downsample(arr):
    """SS -> 1: an exact box average in PREMULTIPLIED alpha, kept in float throughout.

    Premultiplied, so the transparent side cannot bleed into the edges. Float throughout,
    because the obvious version - premultiply, round to 8-bit, resize, un-premultiply - blows
    up on the silhouette: dividing a rounded premultiplied colour by a near-zero alpha
    amplifies the rounding error to 255, and the sprite comes out with a WHITE FRINGE. Which
    is precisely the artifact this whole tool exists to remove, so shipping a sample that had
    one would be its own joke.
    """
    f = arr.astype(np.float64)
    alpha = f[..., 3] / 255.0
    pre = (f[..., :3] * alpha[..., None]).reshape(H, SS, W, SS, 3).mean(axis=(1, 3))
    a = alpha.reshape(H, SS, W, SS).mean(axis=(1, 3))[..., None]
    rgb = np.divide(pre, a, out=np.zeros_like(pre), where=a > 1e-6)
    return np.dstack([np.clip(rgb, 0, 255), np.clip(a[..., 0] * 255.0, 0, 255)]).astype(np.uint8)


def frame(index):
    big = Image.new('RGBA', (W * SS, H * SS), (0, 0, 0, 0))
    draw_figure(ImageDraw.Draw(big), bob=(0, 1, 2, 2, 1, 0, -1, -1)[index])
    arr = np.array(big)

    holes = enclosed_holes(arr[..., 3])
    if not holes.any():
        raise SystemExit(f'frame {index}: the arms did not close a loop - nothing to leave '
                         f'behind, so the sample would not show the defect')
    if index in DIRTY:                      # background removal never reached in here
        arr[..., :3][holes] = 255
        arr[..., 3][holes] = 255
    return downsample(arr), int(holes.sum() // (SS * SS))


def main():
    sheet = Image.new('RGBA', (W * FRAMES, H), (0, 0, 0, 0))
    sizes = []
    for i in range(FRAMES):
        px, area = frame(i)
        sheet.paste(Image.fromarray(px, 'RGBA'), (i * W, 0))
        sizes.append(area)
    png = HERE / 'sample_sheet.png'
    sheet.save(png)

    # A Unity .meta, trimmed to what the tool reads: the sliced sprite rects. Unity measures y
    # from the BOTTOM, which is why the tool flips it.
    rects = ''.join(
        f'    - serializedVersion: 2\n'
        f'      name: sample_sheet_{i}\n'
        f'      rect:\n'
        f'        serializedVersion: 2\n'
        f'        x: {i * W}\n'
        f'        y: 0\n'
        f'        width: {W}\n'
        f'        height: {H}\n'
        for i in range(FRAMES))
    (HERE / 'sample_sheet.png.meta').write_text(
        'fileFormatVersion: 2\n'
        'guid: 00000000000000000000000000000000\n'
        'TextureImporter:\n'
        '  spriteMode: 2\n'
        '  spriteSheet:\n'
        '    sprites:\n' + rects)
    print(f'wrote {png} ({W * FRAMES}x{H}, {FRAMES} frames)')
    print(f'  enclosed holes per frame (px): {sizes}')
    print(f'  left opaque white on frames {sorted(DIRTY)}, transparent on the rest')


if __name__ == '__main__':
    main()
