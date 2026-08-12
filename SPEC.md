# Sprite Pocket Cleaner specification

## Purpose

Remove a manually selected opaque source-matte pocket from an enclosed gap in a PNG sprite
sheet, without making automatic claims about what is artwork.

## Safety invariants

1. **Artist in the loop.** The tool never automatically applies a candidate from `--scan`.
   A user chooses every mark and reviews the browser preview before writing pixels.
2. **Connected and bounded.** A point mark uses 4-connected pixels within tolerance of its seed,
   stops at transparent pixels, and never crosses a frame rectangle.
3. **Artwork guard by default.** Dark or saturated seeds are rejected unless the user explicitly
   approves that individual browser mark, or deliberately passes `--any-colour` on the CLI.
4. **Conservative propagation.** Propagation is opt-in for direct CLI marks. A propagated
   candidate must be near its mapped location and no more than four times the original mark's
   area; the artist reviews every propagated region in the dry-run preview.
5. **Reviewable writes.** The browser writes no pixels. The Python applier owns writes, creates
   one non-overwritten backup, and creates a before/after preview unless asked not to.
6. **Rendering-safe erasure.** Erased pixels have alpha cleared, feathered edges, and padded RGB
   so transparent matte colours do not reappear through filtering or mipmaps.
7. **Preview parity.** The shipped browser page and Python applier must produce identical fill
   regions for the parity fixture.

## Change protocol

Before changing behaviour, add a short acceptance case to the pull request or issue:

- **Must change:** observable intended result.
- **Must not change:** relevant invariants and nearby artwork.
- **Fixture:** smallest synthetic sheet that exhibits both.
- **Test:** add or update a regression test before merging.

Run all checks before merging:

```bash
python3 sprite_pocket_cleaner.py --self-test
python3 sprite_pocket_cleaner.py --parity
python3 -m unittest discover -s tests
```

## Non-goals

- Automatically deciding whether an eye, highlight, garment, or matte blob should be erased.
- Editing Unity metadata, moving geometry, or supporting rotated/trimmed atlas frames.
- Full colour management beyond ordinary sRGB byte values.
