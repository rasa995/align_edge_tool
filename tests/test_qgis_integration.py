# -*- coding: utf-8 -*-
"""Integration tests with QGIS: memory layers, a real map canvas, the
toolbar, the map tool, the batch dialog and plugin load/unload.

Run with the Python of QGIS (skipped without QGIS), from the plugin folder:

    "C:\\Program Files\\QGIS 3.44.x\\bin\\python-qgis-ltr.bat" -m unittest discover -s tests
    "C:\\Program Files\\QGIS 4.x\\bin\\python-qgis.bat" -m unittest discover -s tests
"""

import importlib
import math
import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from qgis.core import (
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsFeature,
        QgsGeometry,
        QgsPointXY,
        QgsProject,
        QgsRectangle,
        QgsVectorLayer,
        QgsWkbTypes,
    )
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtCore import QObject, pyqtSignal
    from qgis.PyQt.QtWidgets import QMainWindow
    HAVE_QGIS = True
except ImportError:
    HAVE_QGIS = False

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = None
SQUARE = "Polygon((0 0, 10 0, 10 10, 0 10, 0 0))"
REF = "Polygon((10.5 -5, 20 -5, 20 15, 10.5 15, 10.5 -5))"
CRS = "EPSG:3857"


def plugin_package():
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
    try:
        return importlib.import_module(os.path.basename(PLUGIN_DIR))
    finally:
        sys.path.pop(0)


def layer(geom_type, name, wkts, crs=CRS, editable=True):
    lyr = QgsVectorLayer("{}?crs={}".format(geom_type, crs), name, "memory")
    feats = []
    for wkt in wkts:
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    if editable:
        lyr.startEditing()
    return lyr


def geom(lyr, fid=None):
    feats = list(lyr.getFeatures())
    f = feats[0] if fid is None else lyr.getFeature(fid)
    return f.geometry()


class MessageBar:
    """Records the messages the plugin pushes to the QGIS message bar."""

    def __init__(self, messages):
        self.messages = messages

    def pushMessage(self, _title, text, level=None, duration=None):  # noqa: N802
        self.messages.append(text)


if HAVE_QGIS:
    class Iface(QObject):
        currentLayerChanged = pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self.window = QMainWindow()
            self.canvas = QgsMapCanvas(self.window)
            self.canvas.resize(800, 600)
            self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(CRS))
            self.canvas.setExtent(QgsRectangle(-10, -10, 30, 20))
            self.messages = []
            self.bar = MessageBar(self.messages)

        def mapCanvas(self):
            return self.canvas

        def messageBar(self):
            return self.bar

        def activeLayer(self):
            return None

        def mainWindow(self):
            return self.window

        def statusBarIface(self):
            raise AttributeError("no status bar in tests")

        def addToolBar(self, tb):
            self.window.addToolBar(tb)

        def addPluginToVectorMenu(self, _menu, _action):
            pass

        def removePluginVectorMenu(self, _menu, _action):
            pass


@unittest.skipUnless(HAVE_QGIS, "QGIS is not available")
class Integration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        global APP                                  # pylint: disable=global-statement
        if QgsApplication.instance() is None:
            APP = QgsApplication([], True)
            APP.initQgis()
        cls.pkg = plugin_package()
        cls.tool_mod = importlib.import_module(cls.pkg.__name__ + ".align_tool")

    def setUp(self):
        QgsProject.instance().clear()
        self.iface = Iface()
        self.plugin = self.pkg.classFactory(self.iface)
        self.plugin.initGui()
        self.tb = self.plugin.toolbar
        self.tool = self.plugin.tool
        self.tb.sp_tol.setValue(2.0)
        self.tb.sp_eps.setValue(0.001)
        self.tb.act_edge.setChecked(True)
        for act in (self.tb.act_other, self.tb.act_junc, self.tb.act_nbr):
            act.setChecked(True)

    def tearDown(self):
        self.plugin.unload()
        QgsProject.instance().clear()

    def tick(self, *layers):
        self.tb.refresh_layers()
        ids = {l.id() for l in layers}
        for i in range(self.tb.lst.count()):
            item = self.tb.lst.item(i)
            on = item.data(self.tool_mod.Qt.ItemDataRole.UserRole) in ids
            item.setCheckState(self.tool_mod.Qt.CheckState.Checked if on
                               else self.tool_mod.Qt.CheckState.Unchecked)

    def click(self, x, y, alt=0):
        cfg = self.tb.current_settings()
        self.tool._align_clicked_edge(QgsPointXY(x, y), cfg, alt)

    def max_x(self, lyr):
        g = geom(lyr)
        return max(v.x() for v in g.vertices())

    # ---------------------------------------------------------------- UI
    def test_toolbar_is_one_button_with_menu(self):
        self.assertEqual(len(self.tb.actions()), 1)
        texts = [a.text() for a in self.tb.menu.actions() if a.text()]
        self.assertIn("Align all within tolerance…", texts)
        self.assertTrue(any(t.startswith("Mode:") for t in texts))
        self.assertTrue(any(t.startswith("References") for t in texts))

    # ---------------------------------------------------------------- 2D
    def test_clicked_edge_2d(self):
        a = layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.click(10, 5)
        self.assertAlmostEqual(geom(a).area(), 105.0)
        self.assertEqual(self.max_x(a), 10.5)
        self.assertEqual(a.undoStack().count(), 1)
        self.assertEqual(QgsWkbTypes.displayString(geom(a).wkbType()), "Polygon")

    def test_selected_features_mode(self):
        a = layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        a.selectAll()
        self.tb.act_sel.setChecked(True)
        self.tool._align_selected(QgsPointXY(10.5, 5), self.tb.current_settings())
        self.assertAlmostEqual(geom(a).area(), 105.0)

    def test_coincident_line_moves_along(self):
        a = layer("Polygon", "edited", [SQUARE])
        line = layer("LineString", "line", ["LineString(10 0, 10 10)"])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.click(9.99, 5)
        self.assertAlmostEqual(geom(a).area(), 105.0)
        self.assertTrue(all(abs(v.x() - 10.5) < 1e-9 for v in geom(line).vertices()))

    # ---------------------------------------------------------------- Z / M
    def test_z_and_m_are_kept_and_interpolated(self):
        a = layer("PolygonZM", "edited",
                  ["PolygonZM((0 0 1 5, 10 0 2 6, 10 10 3 7, 0 10 4 8, 0 0 1 5))"])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.click(10, 5)
        g = geom(a)
        self.assertTrue(QgsWkbTypes.hasZ(g.wkbType()) and QgsWkbTypes.hasM(g.wkbType()))
        self.assertAlmostEqual(g.area(), 105.0)
        zs = {(round(v.x(), 6), round(v.y(), 6)): (v.z(), v.m()) for v in g.vertices()}
        self.assertEqual(zs[(0.0, 0.0)], (1.0, 5.0))       # untouched vertices keep Z/M
        self.assertEqual(zs[(0.0, 10.0)], (4.0, 8.0))
        self.assertEqual(zs[(10.5, 0.0)], (2.0, 6.0))      # from the original (10 0)
        self.assertEqual(zs[(10.5, 10.0)], (3.0, 7.0))

    def test_z_line(self):
        a = layer("LineStringZ", "edited", ["LineStringZ(10 -2 0, 10 12 14)"])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.click(10, 5)
        g = geom(a)
        self.assertTrue(QgsWkbTypes.hasZ(g.wkbType()))
        for v in g.vertices():
            if abs(v.x() - 10.5) < 1e-9:
                self.assertAlmostEqual(v.z(), v.y() + 2.0, places=6)    # z grows with y

    # ---------------------------------------------------------------- curve types
    def test_curve_layer_without_arcs(self):
        a = layer("CurvePolygon", "edited",
                  ["CurvePolygon(CompoundCurve((0 0, 10 0, 10 10, 0 10, 0 0)))"])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.click(10, 5)
        g = geom(a)
        self.assertTrue(QgsWkbTypes.isCurvedType(g.wkbType()))
        self.assertAlmostEqual(g.area(), 105.0)

    def test_arcs_are_refused(self):
        a = layer("CurvePolygon", "edited",
                  ["CurvePolygon(CompoundCurve(CircularString(0 0, 5 -2, 10 0),"
                   "(10 0, 10 10, 0 10, 0 0)))"])
        before = geom(a).asWkt()
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.click(10, 5)
        self.assertEqual(geom(a).asWkt(), before)

    # ---------------------------------------------------------------- CRS
    def test_reference_in_another_crs(self):
        a = layer("Polygon", "edited", [SQUARE])
        g = QgsGeometry.fromWkt(REF)
        g.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem(CRS),
                                           QgsCoordinateReferenceSystem("EPSG:4326"),
                                           QgsProject.instance()))
        ref = layer("Polygon", "ref", [g.asWkt(12)], crs="EPSG:4326", editable=False)
        self.tick(ref)
        self.click(10, 5)
        self.assertAlmostEqual(self.max_x(a), 10.5, places=4)
        self.assertAlmostEqual(geom(a).area(), 105.0, places=2)

    # ---------------------------------------------------------------- preview / Tab
    def test_preview_box(self):
        a = layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        info = self.tool.preview_at(QgsPointXY(10, 5))
        self.assertTrue(info["ok"])
        self.assertIn("Click to align", info["html"])
        self.assertIn("'edited' feature", info["html"])
        self.assertAlmostEqual(info["area"], 5.0)
        self.assertAlmostEqual(info["h"], 0.5)
        self.assertEqual(geom(a).area(), 100.0, "the preview must not write anything")
        self.assertEqual(a.undoStack().count(), 0)

    def test_preview_says_why_not(self):
        layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.tb.sp_tol.setValue(0.2)
        info = self.tool.preview_at(QgsPointXY(10, 5))
        self.assertFalse(info["ok"])
        self.assertIn("No reference boundary", info["html"])

    def test_tab_picks_another_reference(self):
        a = layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF, "Polygon((11.2 -6, 25 -6, 25 16, 11.2 16, 11.2 -6))"],
                    editable=False)
        self.tick(ref)
        info = self.tool.preview_at(QgsPointXY(10, 5), alt=1)
        self.assertIn("Reference 2 of 2", info["html"])
        self.click(10, 5, alt=1)
        self.assertAlmostEqual(self.max_x(a), 11.2)

    def test_tab_zero_is_the_default_choice(self):
        a = layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF, "Polygon((11.2 -6, 25 -6, 25 16, 11.2 16, 11.2 -6))"],
                    editable=False)
        self.tick(ref)
        self.click(10, 5)
        self.assertEqual(self.max_x(a), 10.5)

    def test_hover_and_tab_key(self):
        from qgis.PyQt.QtCore import QEvent, Qt
        from qgis.PyQt.QtGui import QKeyEvent
        from qgis.PyQt.QtWidgets import QApplication
        layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF, "Polygon((11.2 -6, 25 -6, 25 16, 11.2 16, 11.2 -6))"],
                    editable=False)
        self.tick(ref)
        self.iface.canvas.setMapTool(self.tool)
        self.tool._hover_key = ("x",)
        self.tool._hover_pt = QgsPointXY(10, 5)
        QApplication.sendEvent(self.iface.canvas, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab,
                                                            Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.tool._alt, 1)
        self.tool._hover_compute()          # shows the info box
        self.assertFalse(self.tool.rb_gap.asGeometry().isEmpty())
        box = self.tool.info_box
        self.assertFalse(box.isHidden())
        self.assertIn("Reference 2 of 2", box.text())
        self.assertLessEqual(box.width(), box.MAX_WIDTH)
        # it must not hide by itself (a Qt tooltip would, e.g. on a tooltip event)
        from qgis.PyQt.QtGui import QHelpEvent
        from qgis.PyQt.QtCore import QPoint
        QApplication.sendEvent(self.iface.canvas, QHelpEvent(QEvent.Type.ToolTip, QPoint(5, 5),
                                                             QPoint(5, 5)))
        QApplication.processEvents()
        self.assertFalse(box.isHidden())
        QApplication.sendEvent(self.iface.canvas, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Backtab,
                                                            Qt.KeyboardModifier.ShiftModifier))
        self.assertEqual(self.tool._alt, 0)
        self.iface.canvas.unsetMapTool(self.tool)
        self.assertIsNone(self.tool._hover_key)
        self.assertTrue(box.isHidden())

    # ---------------------------------------------------------------- writing safety
    def test_rollback_when_a_layer_fails(self):
        a = layer("Polygon", "edited", [SQUARE])
        line = layer("LineString", "line", ["LineString(10 0, 10 10)"])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)

        def boom(*_args):
            raise RuntimeError("disk full")
        line.changeGeometry = boom
        self.click(9.99, 5)
        self.assertEqual(geom(a).area(), 100.0, "the first layer must be taken back")
        self.assertTrue(any("taken back" in m for m in self.iface.messages))

    def test_stale_feature_is_not_written(self):
        a = layer("Polygon", "edited", [SQUARE])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        cfg = self.tb.current_settings()
        res, _msg = self.tool._resolve_clicked(QgsPointXY(10, 5), cfg)
        hit, state, key, _e, _ring, rf, crs = res
        collect = self.tool._new_collect()
        self.tool._run_align(state, key, hit.pi, hit.ri, rf, cfg, crs, hit.q, collect)
        fid = next(a.getFeatures()).id()
        a.changeGeometry(fid, QgsGeometry.fromWkt("Polygon((0 0, 9 0, 9 9, 0 9, 0 0))"))
        ok, msg = self.tool._commit(state, "Align edge", collect["primary"])
        self.assertFalse(ok)
        self.assertIn("in the meantime", msg)
        self.assertAlmostEqual(geom(a).area(), 81.0)

    # ---------------------------------------------------------------- batch
    def test_batch_scan_and_align(self):
        a = layer("Polygon", "edited", [SQUARE, "Polygon((0 11, 10.2 11, 10.2 14, 0 14, 0 11))",
                                         "Polygon((-30 0, -20 0, -20 5, -30 0))"])
        ref = layer("Polygon", "ref", [REF], editable=False)
        self.tick(ref)
        self.plugin._open_batch()
        dlg = self.plugin.batch_dialog
        dlg.cb_base.setCurrentIndex(dlg.cb_base.findData(ref.id()))
        dlg.cb_area.setCurrentIndex(1)
        dlg.scan()
        self.assertEqual(len(dlg.items), 2)
        dlg.align()
        self.assertEqual(dlg.table.item(0, 5).text()[:7], "Aligned")
        xs = sorted(max(v.x() for v in f.geometry().vertices()) for f in a.getFeatures())
        self.assertEqual(xs, [-20.0, 10.5, 10.5])
        dlg.scan()
        self.assertEqual(len(dlg.items), 0, "nothing left to align")


if __name__ == "__main__":
    unittest.main()
