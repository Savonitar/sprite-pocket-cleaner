#!/usr/bin/env python3
"""Regenerate before-after.png (the README image) from the sample sheet.

Runs the real tool on a COPY of the sample, so the shipped sample keeps its defect - the
quickstart in README.md depends on that pocket still being there.

Run:  python3 sample/make_before_after.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FRAMES = (2, 3, 4, 5)          # the frames that carry the defect
W, H, SCALE = 96, 120, 4


# Flat magenta, not a checkerboard: the pocket IS white, and white-on-checkerboard is exactly
# the comparison the eye gets wrong (I misread my own first attempt). The tool's own preview
# composites over magenta for the same reason.
VOID = (255, 0, 220)


def backdrop(w, h):
    return Image.new('RGB', (w, h), VOID)


def strip(arr):
    out = Image.new('RGBA', (len(FRAMES) * W, H), (0, 0, 0, 0))
    for k, f in enumerate(FRAMES):
        out.alpha_composite(Image.fromarray(arr[:, f * W:(f + 1) * W], 'RGBA'), (k * W, 0))
    return out


def render(layer, w, h):
    bg = backdrop(w, h).convert('RGBA')
    bg.alpha_composite(layer.resize((w, h), Image.NEAREST))
    return bg.convert('RGB')


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copy(HERE / 'sample_sheet.png', tmp / 's.png')
        shutil.copy(HERE / 'sample_sheet.png.meta', tmp / 's.png.meta')
        subprocess.run([sys.executable, str(ROOT / 'sprite_pocket_cleaner.py'), str(tmp / 's.png'),
                        '--at', '317,75', '--at', '355,75',
                        '--propagate',
                        '--no-preview', '--no-backup'],
                       check=True, capture_output=True)
        before = np.array(Image.open(HERE / 'sample_sheet.png').convert('RGBA'))
        after = np.array(Image.open(tmp / 's.png').convert('RGBA'))
    finally:
        shutil.rmtree(tmp)

    top, bottom = strip(before), strip(after)
    w, h = top.width * SCALE, top.height * SCALE
    label = 30
    img = Image.new('RGB', (w, h * 2 + label * 2 + 10), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((10, 9), 'BEFORE   opaque white filling the holes enclosed by the arms (frames 2-5)',
           fill=(170, 25, 25))
    img.paste(render(top, w, h), (0, label))
    d.text((10, label + h + 13),
           'AFTER   erased - the background shows through; eyes and art untouched',
           fill=(20, 115, 60))
    img.paste(render(bottom, w, h), (0, label * 2 + h + 10))
    out = ROOT / 'before-after.png'
    img.save(out)
    print(f'wrote {out} {img.size}')


if __name__ == '__main__':
    main()
