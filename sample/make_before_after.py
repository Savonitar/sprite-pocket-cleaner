#!/usr/bin/env python3
"""Regenerate before-after.png from the bundled real-world Office Cat fixture.

The real applier runs on a temporary copy, so sample_sheet.png deliberately keeps the opaque
gray matte trapped inside the tail curl.

Run:  python3 sample/make_before_after.py
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = HERE / 'sample_sheet.png'
SOURCE_META = HERE / 'sample_sheet.png.meta'
EXPECTED_SHA256 = '84310781e847829d28c408145366fbe42d2065d683980451fa72d6cd08be4acf'
EXPECTED_META_SHA256 = '2b64d12f4da6ebbd7842ef0a2ba0dcddc5fa27d9bbd4c67a938d0d709bc92869'

CELL = 512
FRAME = 0
MARK = (330, 305)
CROP = (110, 80, 410, 450)
LABEL_H = 26
GAP = 8
SCALE = 2
VOID = (255, 0, 220)


def source_frame(sheet):
    col, row = FRAME % 5, FRAME // 5
    return sheet.crop((col * CELL, row * CELL, (col + 1) * CELL, (row + 1) * CELL))


def over_void(layer):
    bg = Image.new('RGBA', layer.size, (*VOID, 255))
    bg.alpha_composite(layer)
    return bg.convert('RGB')


def main():
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    meta_digest = hashlib.sha256(SOURCE_META.read_bytes()).hexdigest()
    if digest != EXPECTED_SHA256 or meta_digest != EXPECTED_META_SHA256:
        raise SystemExit(
            'the Office Cat sample or metadata no longer matches the reviewed fixture; '
            'restore both before regenerating the documentation image')

    tmp = Path(tempfile.mkdtemp(prefix='sprite-pocket-cleaner-'))
    try:
        dirty = tmp / 'office_cat.png'
        shutil.copy(SOURCE, dirty)
        shutil.copy(SOURCE_META, tmp / 'office_cat.png.meta')
        subprocess.run(
            [
                sys.executable,
                str(ROOT / 'sprite_pocket_cleaner.py'),
                str(dirty),
                '--at',
                f'{MARK[0]},{MARK[1]}',
                '--any-colour',
                '--no-preview',
                '--no-backup',
            ],
            check=True,
            capture_output=True,
        )
        before = source_frame(Image.open(SOURCE).convert('RGBA')).crop(CROP)
        after = source_frame(Image.open(dirty).convert('RGBA')).crop(CROP)
    finally:
        shutil.rmtree(tmp)

    panel_w, panel_h = before.size
    image = Image.new('RGB', (panel_w * 2 + GAP, panel_h + LABEL_H), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((6, 7), 'BEFORE  gray matte in tail loop', fill=(170, 25, 25))
    draw.text((panel_w + GAP + 6, 7), 'AFTER  transparent loop', fill=(20, 115, 60))
    image.paste(over_void(before), (0, LABEL_H))
    image.paste(over_void(after), (panel_w + GAP, LABEL_H))

    image = image.resize((image.width * SCALE, image.height * SCALE), Image.Resampling.NEAREST)
    out = ROOT / 'before-after.png'
    image.save(out, optimize=True)
    print(f'wrote {out} {image.size}')


if __name__ == '__main__':
    main()
