# -*- coding: utf-8 -*-
"""Tests of the geometry core (align_core.py). No QGIS needed:

    python -m unittest discover -s tests        (from the plugin folder)
"""

import importlib.util
import math
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "align_core", os.path.join(os.path.dirname(_HERE), "align_core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
TOL, EPS = 2.0, 0.001


def ref_right(x=10.5):
    """Reference polygon ring whose left side runs along x, next to SQUARE."""
    return core.Linework([(x, -5.0), (20.0, -5.0), (20.0, 15.0), (x, 15.0), (x, -5.0)],
                         True, 2.0 * TOL)


class BasicHelpers(unittest.TestCase):

    def test_polygon_area(self):
        self.assertAlmostEqual(core.polygon_area(SQUARE), 100.0)
        self.assertAlmostEqual(core.polygon_area(SQUARE + SQUARE[:1]), 100.0)
        self.assertAlmostEqual(core.polygon_area(SQUARE[::-1]), 100.0)

    def test_ring_drops_closing_vertex(self):
        r = core.Ring(SQUARE + SQUARE[:1], True)
        self.assertEqual(r.n, 4)
        self.assertEqual(r.to_list()[-1], r.to_list()[0])

    def test_hausdorff(self):
        self.assertAlmostEqual(core.hausdorff([(0, 0), (10, 0)], [(0, 1), (10, 1)]), 1.0)


class CarryZM(unittest.TestCase):
    ORIG = [(0.0, 0.0, 1.0, 10.0), (10.0, 0.0, 3.0, 30.0)]

    def test_existing_vertex_keeps_values(self):
        self.assertEqual(core.carry_zm(self.ORIG, [(10.0, 0.0)]), [(3.0, 30.0)])

    def test_new_vertex_is_interpolated(self):
        z, m = core.carry_zm(self.ORIG, [(5.0, 0.4)])[0]
        self.assertAlmostEqual(z, 2.0)
        self.assertAlmostEqual(m, 20.0)

    def test_beyond_the_end_takes_the_end_value(self):
        z, _m = core.carry_zm(self.ORIG, [(12.0, 0.0)])[0]
        self.assertAlmostEqual(z, 3.0)

    def test_unknown_stays_unknown(self):
        orig = [(0.0, 0.0, 1.0, float("nan")), (10.0, 0.0, 3.0, float("nan"))]
        z, m = core.carry_zm(orig, [(5.0, 0.0)])[0]
        self.assertAlmostEqual(z, 2.0)
        self.assertTrue(math.isnan(m))


class AlignRing(unittest.TestCase):

    def test_clicked_edge_moves_onto_reference(self):
        ring = core.Ring(SQUARE, True)
        res = core.align_ring(ring, ref_right(), TOL, EPS, click_point=(10.0, 5.0))
        self.assertIsNotNone(res.ring)
        self.assertAlmostEqual(core.polygon_area(res.ring.pts), 105.0)
        xs = sorted(p[0] for p in res.ring.pts)
        self.assertEqual(xs[-1], 10.5)
        self.assertEqual(ring.pts, SQUARE, "the input ring must not be modified")

    def test_too_far_is_not_aligned(self):
        res = core.align_ring(core.Ring(SQUARE, True), ref_right(13.0), TOL, EPS,
                              click_point=(10.0, 5.0))
        self.assertIsNone(res.ring)

    def test_already_aligned(self):
        res = core.align_ring(core.Ring(SQUARE, True), ref_right(10.0), TOL, EPS,
                              click_point=(10.0, 5.0))
        self.assertIsNone(res.ring)

    def test_all_stretches_mode(self):
        res = core.align_ring(core.Ring(SQUARE, True), ref_right(), TOL, EPS)
        self.assertIsNotNone(res.ring)
        self.assertAlmostEqual(core.polygon_area(res.ring.pts), 105.0)

    def test_neighbour_moves_along(self):
        # a line lying on the moved edge follows it
        line = core.Ring([(10.0, 0.0), (10.0, 10.0)], False)

        def provider(_bb, done=[]):
            if done:
                return []
            done.append(1)
            return [core.NeighbourItem(line, False, "line")]

        res = core.align_ring(core.Ring(SQUARE, True), ref_right(), TOL, EPS,
                              click_point=(10.0, 5.0), neighbour_provider=provider)
        self.assertEqual(len(res.neighbours), 1)
        _it, new = res.neighbours[0]
        self.assertTrue(all(abs(p[0] - 10.5) < 1e-9 for p in new.pts))

    def test_build_path_statuses(self):
        run = [(10.0, 0.0), (10.0, 10.0)]
        self.assertEqual(core.build_path(ref_right(), run, TOL, EPS)[1], "ok")
        self.assertEqual(core.build_path(ref_right(10.0), run, TOL, EPS)[1], "aligned")
        self.assertEqual(core.build_path(ref_right(13.0), run, TOL, EPS)[1], "far")


if __name__ == "__main__":
    unittest.main()
