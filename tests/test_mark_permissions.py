"""Regression tests for manually approved dark/coloured matte marks."""

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import numpy as np

import sprite_pocket_cleaner as eraser


class MarkPermissionTests(unittest.TestCase):
    def test_dark_gray_requires_explicit_permission(self):
        gray = (100, 100, 100)

        self.assertFalse(eraser.seed_is_allowed(gray))
        self.assertTrue(eraser.seed_is_allowed(gray, allow_any_colour=True))
        self.assertFalse(eraser.seed_is_allowed((150, 110, 70)))  # fur-like brown

    def test_explicit_gray_pocket_does_not_reach_same_colored_eye(self):
        arr, boxes = eraser._fake_sheet()
        gray = np.array([100, 100, 100], dtype=np.uint8)

        # The target pocket and eye deliberately have exactly the same gray. Only the
        # manually selected, connected pocket may be erased.
        arr[22:30, 24:30, :3] = gray
        arr[10:18, 10:16, :3] = gray
        before = arr.copy()

        mask = eraser.region_at(arr, (26, 24), eraser.DEFAULT_TOLERANCE, boxes[0])
        self.assertIsNotNone(mask)
        self.assertEqual(int(mask.sum()), 48)
        self.assertFalse(mask[10:18, 10:16].any())

        eraser.erase(arr, mask, gray, eraser.DEFAULT_TOLERANCE, eraser.DEFAULT_FEATHER)
        self.assertEqual(int(arr[24, 26, 3]), 0)
        np.testing.assert_array_equal(before[10:18, 10:16], arr[10:18, 10:16])

    def test_plan_preserves_per_mark_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / 'plan.json'
            plan_path.write_text(json.dumps({
                'version': 2,
                'sheet': '/tmp/cat.png',
                'defaults': {'tolerance': 30, 'feather': 2, 'propagate': True},
                'marks': [{'type': 'point', 'x': 20, 'y': 30, 'allowAnyColour': True}],
            }))
            args = Namespace(sheet=None, plan=str(plan_path), feather=None, at=None, rect=None,
                             no_propagate=False, tolerance=eraser.DEFAULT_TOLERANCE)

            sheet, marks = eraser.parse_marks(args)

        self.assertEqual(sheet, '/tmp/cat.png')
        self.assertEqual(len(marks), 1)
        self.assertTrue(marks[0]['allowAnyColour'])
        self.assertEqual(args.feather, 2)

    def test_rectangle_must_stay_inside_its_starting_frame(self):
        _, boxes = eraser._fake_sheet()

        self.assertTrue(eraser.rect_within_frame((24, 22, 6, 8), boxes[0]))
        self.assertFalse(eraser.rect_within_frame((36, 22, 8, 8), boxes[0]))
        self.assertFalse(eraser.rect_within_frame((24, 22, 0, 8), boxes[0]))


if __name__ == '__main__':
    unittest.main()
