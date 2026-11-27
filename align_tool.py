# -*- coding: utf-8 -*-
"""
Map tool of the Edge Aligner plugin (QGIS glue around align_core).

Modes
-----
Clicked edge
    Click an edge of a feature in a layer that is being edited. The nearest
    boundary of a feature in the reference layers (within the alignment
    tolerance) is the reference. The contiguous stretch of the clicked edge
    whose vertices lie within the tolerance is replaced by the matching part
    of the reference boundary.

Selected features
    Select features in layers being edited, then click the reference
    boundary. Every stretch of the selected features within the tolerance of
    that boundary is aligned.

In both modes, features in layers being edited that share the moved stretch
(coincident within the shared-edge tolerance) are moved with it, so shared
boundaries stay identical. The reference feature is never changed.
"""

import html
import traceback

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLineString,
    QgsMessageLog,
    QgsMultiLineString,
    QgsMultiPolygon,
    QgsPointXY,
    QgsPolygon,
    QgsProject,
    QgsRectangle,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QEvent, QPoint, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QCursor, QTextDocument
from qgis.PyQt.QtWidgets import QFrame, QLabel, QToolTip

from . import align_core as core

TITLE = "Edge Aligner"
SEARCH_PIXELS = 10
MAX_NETWORK_VERTICES = 10000   # combined reference: skipped if the area is too busy
LINE = Qgis.GeometryType.Line
POLY = Qgis.GeometryType.Polygon


# --------------------------------------------------------------------------
# Layer / geometry helpers
# --------------------------------------------------------------------------

def is_supported_layer(layer):
    return (isinstance(layer, QgsVectorLayer) and layer.isValid()
            and layer.geometryType() in (LINE, POLY))


def project_layers():
    return [l for l in QgsProject.instance().mapLayers().values() if is_supported_layer(l)]


def editable_layers():
    return [l for l in project_layers() if l.isEditable()]


def log_exception(level=Qgis.MessageLevel.Info):
    """Write the exception being handled to the message log (tab 'Edge Aligner')."""
    QgsMessageLog.logMessage(traceback.format_exc(), TITLE, level)


def geometry_problem(geom):
    if geom is None or geom.isNull() or geom.isEmpty():
        return "empty geometry"
    if QgsWkbTypes.isCurvedType(geom.wkbType()) and geom.constGet().hasCurvedSegments():
        return "geometry with arcs"
    return None


def straight(geom):
    """The same geometry as straight segments: a curve type without arcs
    (CompoundCurve, CurvePolygon, MultiCurve, MultiSurface) becomes the
    equivalent LineString/Polygon type with exactly the same vertices."""
    if QgsWkbTypes.isCurvedType(geom.wkbType()):
        return QgsGeometry(geom.constGet().segmentize())
    return geom


def has_zm(geom):
    wkb = geom.wkbType()
    return QgsWkbTypes.hasZ(wkb) or QgsWkbTypes.hasM(wkb)


def zm_parts(geom):
    """[[[(x, y, z, m), ...] per ring] per part] of a straight geometry with
    Z and/or M values (a missing dimension is NaN); rings keep their closing
    vertex."""
    def pts(curve):
        return [(p.x(), p.y(), p.z(), p.m()) for p in curve.points()]
    out = []
    g = geom.constGet()
    geoms = [g.geometryN(i) for i in range(g.numGeometries())] if geom.isMultipart() else [g]
    for part in geoms:
        if geom.type() == LINE:
            out.append([pts(part)])
        else:
            rings = [pts(part.exteriorRing())]
            rings += [pts(part.interiorRing(i)) for i in range(part.numInteriorRings())]
            out.append(rings)
    return out


def _zm_curve(pts, zm, has_z, has_m):
    vals = core.carry_zm(zm, pts)
    return QgsLineString([p[0] for p in pts], [p[1] for p in pts],
                         [v[0] for v in vals] if has_z else [],
                         [v[1] for v in vals] if has_m else [])


def _zm_geom(kind, parts, zm, has_z, has_m):
    """Geometry with Z/M from the 2D parts; Z/M carried over from the
    original vertices (zm, as zm_parts) and interpolated for new ones."""
    if kind == "line":
        geoms = [_zm_curve(part[0].to_list(), zm[pi][0], has_z, has_m)
                 for pi, part in enumerate(parts)]
        multi = QgsMultiLineString()
    else:
        geoms = []
        for pi, part in enumerate(parts):
            poly = QgsPolygon()
            for ri, ring in enumerate(part):
                curve = _zm_curve(ring.to_list(), zm[pi][ri], has_z, has_m)
                if ri == 0:
                    poly.setExteriorRing(curve)
                else:
                    poly.addInteriorRing(curve)
            geoms.append(poly)
        multi = QgsMultiPolygon()
    if len(geoms) == 1:
        return QgsGeometry(geoms[0])
    for g in geoms:
        multi.addGeometry(g)
    return QgsGeometry(multi)


def geom_to_parts(geom):
    """QgsGeometry -> ('line'|'poly', [[Ring, ...], ...]) in 2D (Z/M and
    curve types without arcs are read as plain XY)."""
    geom = straight(geom)
    if geom.type() == LINE:
        lines = geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
        return "line", [[core.Ring([(p.x(), p.y()) for p in line], False)] for line in lines]
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    return "poly", [[core.Ring([(p.x(), p.y()) for p in ring], True) for ring in poly]
                    for poly in polys]


def parts_to_geom(kind, parts, layer, was_multi, zm=None, has_z=False, has_m=False,
                  curved=False):
    """Geometry from the 2D parts. zm (see zm_parts): the original Z/M values,
    carried over to the new geometry. curved: write a curve type (no arcs)."""
    if kind == "line" and any(len(part[0].to_list()) < 2 for part in parts):
        return None
    if kind != "line" and any(ring.n < 3 for part in parts for ring in part):
        return None
    if zm is not None:
        geom = _zm_geom(kind, parts, zm, has_z, has_m)
    else:
        geom = _xy_geom(kind, parts)
    if geom is None:
        return None
    if curved:
        geom = QgsGeometry(geom.constGet().toCurveType())
    if was_multi or QgsWkbTypes.isMultiType(layer.wkbType()):
        geom.convertToMultiType()
    return geom


def _xy_geom(kind, parts):
    if kind == "line":
        lines = []
        for part in parts:
            pts = part[0].to_list()
            if len(pts) < 2:
                return None
            lines.append([QgsPointXY(x, y) for x, y in pts])
        geom = (QgsGeometry.fromMultiPolylineXY(lines) if len(lines) > 1
                else QgsGeometry.fromPolylineXY(lines[0]))
    else:
        polys = []
        for part in parts:
            rings = []
            for ring in part:
                if ring.n < 3:
                    return None
                rings.append([QgsPointXY(x, y) for x, y in ring.to_list()])
            polys.append(rings)
        geom = (QgsGeometry.fromMultiPolygonXY(polys) if len(polys) > 1
                else QgsGeometry.fromPolygonXY(polys[0]))
    return geom


def rect_request(pt, r):
    rect = QgsRectangle(pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r)
    return QgsFeatureRequest().setFilterRect(rect).setNoAttributes()


def bbox_request(bb):
    return QgsFeatureRequest().setFilterRect(QgsRectangle(*bb)).setNoAttributes()


def rings_of(parts):
    for pi, part in enumerate(parts):
        for ri, ring in enumerate(part):
            yield pi, ri, ring


def feature_parts(layer, request, exclude=()):
    """(feature, geometry, parts) of every feature of `layer` matching
    `request` with a supported geometry; keys (layer id, fid) in `exclude`
    are skipped."""
    lid = layer.id()
    for f in layer.getFeatures(request):
        if (lid, f.id()) in exclude:
            continue
        g = f.geometry()
        if geometry_problem(g):
            continue
        yield f, g, geom_to_parts(g)[1]


def polyline_geom(pts):
    return QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])


def ring_valid(pts):
    """GEOS validity of a closed ring taken as a polygon on its own."""
    if len(pts) < 4:
        return False
    return QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in pts]]).isGeosValid()


def extra_vertices(orig, new, closed):
    """Vertices of `new` that are not in `orig`, if `new` is `orig` with
    vertices inserted (a ring may also start at another vertex). None if
    `new` differs in any other way. Both are point lists as Ring.to_list()."""
    if closed:
        orig, new = orig[:-1], new[:-1]
    if not orig or len(new) < len(orig):
        return None
    if closed:
        starts = [i for i, p in enumerate(new) if p == orig[0]]
    else:
        starts = [0] if new[0] == orig[0] and new[-1] == orig[-1] else []
    n = len(new)
    for s in starts:
        extra, j = [], 0
        for t in range(n):
            p = new[(s + t) % n]
            if j < len(orig) and p == orig[j]:
                j += 1
            else:
                extra.append(p)
        if j == len(orig):
            return extra
    return None


def only_densified(kind, orig_lists, new_lists, keep_pts, eps):
    """True if the feature only got vertices inserted (or a ring starts at
    another vertex) and none of them lies on a newly aligned edge (keep_pts):
    its shape is unchanged and nothing needs to be written."""
    if len(orig_lists) != len(new_lists):
        return False
    for opart, npart in zip(orig_lists, new_lists):
        if len(opart) != len(npart):
            return False
        for o, n in zip(opart, npart):
            extra = extra_vertices(o, n, kind == "poly")
            if extra is None:
                return False
            if any(core.dist(p, k) <= eps for p in extra for k in keep_pts):
                return False
    return True


def _unit(crs):
    """' m', ' ft', ' °' ... for the map units of crs ('' if unknown)."""
    try:
        abbr = QgsUnitTypes.toAbbreviatedString(crs.mapUnits())
    except (AttributeError, TypeError):
        abbr = ""
    return " " + abbr if abbr else ""


def _short_list(items, limit=5):
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + " and {} more".format(len(items) - limit)


def _tip_html(title, lines, warn=False):
    """Text of the info box shown next to the cursor."""
    color = "#c05000" if warn else "#007a33"
    body = "".join("<div style='margin-top:2px'>{}</div>".format(html.escape(str(l), quote=False))
                   for l in lines if l)
    return ("<div><b style='color:{}'>{}</b>{}</div>"
            .format(color, html.escape(title, quote=False), body))


# --------------------------------------------------------------------------
# Working copy of the geometries touched by one operation
# --------------------------------------------------------------------------

class Entry:
    __slots__ = ("layer", "fid", "kind", "parts", "orig_lists", "orig_valid", "was_multi",
                 "orig_geom", "zm", "has_z", "has_m", "curved")

    def new_geometry(self):
        return parts_to_geom(self.kind, self.parts, self.layer, self.was_multi, self.zm,
                             self.has_z, self.has_m, self.curved)


class WorkState:
    def __init__(self):
        self.entries = {}
        self.modified = set()

    def get(self, layer, fid, feature=None):
        key = (layer.id(), fid)
        if key in self.entries:
            return self.entries[key]
        if feature is None:
            feature = layer.getFeature(fid)
        geom = feature.geometry()
        if geometry_problem(geom):
            self.entries[key] = None
            return None
        e = Entry()
        e.layer = layer
        e.fid = fid
        e.kind, e.parts = geom_to_parts(geom)
        e.orig_lists = [[r.to_list() for r in part] for part in e.parts]
        e.orig_valid = geom.isGeosValid()
        e.was_multi = geom.isMultipart()
        e.orig_geom = QgsGeometry(geom)
        wkb = geom.wkbType()
        e.has_z, e.has_m = QgsWkbTypes.hasZ(wkb), QgsWkbTypes.hasM(wkb)
        e.zm = zm_parts(straight(geom)) if (e.has_z or e.has_m) else None
        e.curved = (QgsWkbTypes.isCurvedType(wkb)
                    or QgsWkbTypes.isCurvedType(layer.wkbType()))
        self.entries[key] = e
        return e


class Hit:
    __slots__ = ("layer", "fid", "pi", "ri", "k", "q", "d")


class Reference:
    __slots__ = ("lw", "keys", "layer", "label", "m", "alt", "alts")

    def __init__(self, lw, keys, layer, label, m):
        self.lw = lw          # reference linework (one ring or merged line)
        self.keys = keys      # features that stay fixed (reference + coincident ties)
        self.layer = layer
        self.label = label    # for messages
        self.m = m            # measure on lw nearest to the click (for flashing)
        self.alt = 0          # which of the candidate references (Tab cycles them)
        self.alts = 1         # number of candidate references at the edge


class Cand:
    """Reference candidate."""
    __slots__ = ("d", "pt", "m", "lw", "keys", "layer", "label", "inside")

    def __init__(self, near, lw, keys, layer, label, inside=False):
        self.d, self.m, self.pt = near
        self.lw = lw
        self.keys = keys      # None for merged line chains (computed when needed)
        self.layer = layer
        self.label = label
        self.inside = inside


# --------------------------------------------------------------------------
# Map tool
# --------------------------------------------------------------------------

class InfoBox(QLabel):
    """The hover info box: a label drawn on the map canvas next to the
    cursor. Unlike a Qt tooltip it never hides by itself; the map tool hides
    it when the cursor leaves the edge, on Esc, or when the tool is switched
    off. It lets every mouse event through to the canvas."""
    OFFSET = QPoint(18, 18)
    MAX_WIDTH = 460

    def __init__(self, canvas):
        super().__init__(canvas)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setWordWrap(True)
        self.setMargin(6)
        self.setFrameShape(QFrame.Shape.Box)
        self.setPalette(QToolTip.palette())
        self.setAutoFillBackground(True)
        self._anchor = None
        self.hide()

    def show_text(self, text, anchor=None):
        """Show `text` (HTML) next to the canvas pixel position `anchor`."""
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setHtml(text)
        pad = 2 * (self.margin() + self.frameWidth()) + 4
        width = min(int(doc.idealWidth()) + pad, self.MAX_WIDTH)
        self.setText(text)
        self.setFixedWidth(width)
        self.setFixedHeight(self.heightForWidth(width))
        if anchor is not None:
            self._anchor = anchor
        self._place()
        self.show()
        self.raise_()

    def follow(self, anchor):
        """Move the box along with the cursor (if it is shown)."""
        self._anchor = anchor
        if self.isVisible():
            self._place()

    def _place(self):
        parent = self.parentWidget()
        if parent is None or self._anchor is None:
            return
        pos = self._anchor + self.OFFSET
        # keep the box inside the canvas: flip to the other side of the cursor
        if pos.x() + self.width() > parent.width():
            pos.setX(self._anchor.x() - self.OFFSET.x() - self.width())
        if pos.y() + self.height() > parent.height():
            pos.setY(self._anchor.y() - self.OFFSET.y() - self.height())
        pos.setX(max(0, pos.x()))
        pos.setY(max(0, pos.y()))
        self.move(pos)


class AlignEdgeMapTool(QgsMapTool):

    def __init__(self, iface, controls):
        self.iface = iface
        canvas = iface.mapCanvas()
        super().__init__(canvas)
        self.controls = controls
        self.rb = QgsRubberBand(canvas, LINE)
        self.rb.setColor(QColor(230, 0, 230, 200))
        self.rb.setWidth(4)
        # hover preview: the stretch that will move (solid, rb) and the part of
        # the reference it will be moved onto (dashed, rb_target)
        self.rb_target = QgsRubberBand(canvas, LINE)
        self.rb_target.setColor(QColor(255, 120, 0, 230))
        self.rb_target.setWidth(3)
        self.rb_target.setLineStyle(Qt.PenStyle.DashLine)
        # the area between the stretch and the reference (shaded)
        self.rb_gap = QgsRubberBand(canvas, POLY)
        self.rb_gap.setColor(QColor(255, 120, 0, 60))
        self.rb_gap.setStrokeColor(QColor(255, 120, 0, 0))
        # what the click will do: a box on the canvas next to the cursor that
        # stays while the edge is hovered (a Qt tooltip would hide by itself)
        self.info_box = InfoBox(canvas)
        self._hover_key = None
        self._hover_pt = None
        self._alt = 0                   # candidate reference chosen with Tab
        self._work_crs = None           # CRS the current operation works in
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._hover_compute)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    # ------------------------------------------------------------ events
    def _clear_hover(self):
        self._hover_key = None
        self._hover_timer.stop()
        self.rb.reset(LINE)
        self.rb_target.reset(LINE)
        self.rb_gap.reset(POLY)
        self.info_box.hide()

    def activate(self):
        super().activate()
        self.controls.update_units()
        self.canvas().installEventFilter(self)

    def deactivate(self):
        self._clear_hover()
        self.canvas().removeEventFilter(self)
        super().deactivate()

    def cleanup(self):
        """Plugin unload: stop the hover preview and take the rubber bands off the canvas."""
        self._clear_hover()
        self.canvas().removeEventFilter(self)
        scene = self.canvas().scene()
        for band in (self.rb, self.rb_target, self.rb_gap):
            if scene is not None and band.scene() is scene:
                scene.removeItem(band)
        self.info_box.hide()
        self.info_box.setParent(None)
        self.info_box.deleteLater()

    def eventFilter(self, obj, event):
        """Tab / Shift+Tab while hovering an edge: next / previous candidate
        reference. (The canvas would otherwise use Tab to move the focus.)"""
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
                and self._hover_key is not None):
            back = (event.key() == Qt.Key.Key_Backtab
                    or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            self._alt += -1 if back else 1
            self._hover_timer.start(0)
            return True
        return False

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self._clear_hover()

    def canvasMoveEvent(self, e):
        if self.controls.mode() != self.controls.MODE_EDGE:
            self._clear_hover()
            return
        try:
            hit = self._pick_target_segment(e.mapPoint(), editable_layers(),
                                            self.controls.current_settings().eps)
        except Exception:  # pylint: disable=broad-exception-caught
            hit = None     # hover only: the same error is reported in full on the click
        if hit is None:
            self._clear_hover()
            return
        key = (hit.layer.id(), hit.fid, hit.pi, hit.ri, hit.k)
        self.info_box.follow(self.toCanvasCoordinates(e.mapPoint()))
        if key == self._hover_key:
            return
        e_ = WorkState().get(hit.layer, hit.fid)
        if e_ is None:
            self._clear_hover()
            return
        self._hover_key = key
        self._hover_pt = e.mapPoint()
        self._alt = 0
        self.controls.update_units()    # an edited layer may have another unit than the last one
        # immediate feedback: the hovered segment; the full preview follows
        a, b = e_.parts[hit.pi][hit.ri].seg(hit.k)
        self.rb.setToGeometry(polyline_geom([a, b]), hit.layer)
        self.rb_target.reset(LINE)
        self.rb_gap.reset(POLY)
        self._hover_timer.start(150)

    def _hover_compute(self):
        """Preview of the click at the hovered segment: the stretch that would
        move (solid), the part of the reference it would go to (dashed), the
        area in between (shaded), and an info box next to the cursor saying
        what the click will do. The click is computed in full (without
        writing), so the box also says when the click would be refused."""
        key = self._hover_key
        if key is None or self._hover_pt is None:
            return
        try:
            info = self.preview_at(self._hover_pt, self._alt, key)
        except Exception:  # pylint: disable=broad-exception-caught
            return         # hover only: the same error is reported in full on the click
        if info is None or self._hover_key != key:
            return
        self._show_tip(info["html"])

    def preview_at(self, map_pt, alt=0, key=None):
        """Compute the preview of a click at map_pt (clicked-edge mode) and
        show it on the canvas. Returns {"ok": bool, "html": info box text,
        ...} or None if there is nothing to show."""
        cfg = self.controls.current_settings()
        if cfg.tol <= cfg.eps:
            return {"ok": False, "html": _tip_html(
                "Cannot align", ["The tolerance must be larger than the shared-edge "
                                 "tolerance."], warn=True)}
        res, msg = self._resolve_clicked(map_pt, cfg, alt)
        if key is not None and self._hover_key != key:
            return None
        if res is None:
            return {"ok": False, "html": _tip_html("Cannot align", [msg[0]], warn=True)}
        hit, state, k, _entry, ring, ref, crs = res
        unit = _unit(crs)
        lines = ["Reference: " + ref.label]
        if ref.alts > 1:
            lines.append("Reference {} of {} (Tab / Shift+Tab for another)"
                         .format(ref.alt % ref.alts + 1, ref.alts))
        work = ring.copy()
        st, why = core.click_stretch(work, ref.lw, cfg.tol, cfg.eps, hit.q)
        run = None
        if st is not None:
            run = core.locate_run(work, core.anchor_at(work, st[0], cfg.eps),
                                  core.anchor_at(work, st[1], cfg.eps))
        if run is None:
            self.rb_target.reset(LINE)
            self.rb_gap.reset(POLY)
            return {"ok": False, "html": _tip_html(
                "Nothing to align here", lines + [why or "No stretch to align."], warn=True)}
        run_pts = work.run_points(*run)
        path, status, h = core.build_path(ref.lw, run_pts, cfg.tol, cfg.eps)
        self.rb.setToGeometry(polyline_geom(run_pts), hit.layer)
        if path is None:
            self.rb_target.reset(LINE)
            self.rb_gap.reset(POLY)
            return {"ok": False, "html": _tip_html(
                "Nothing to align here",
                lines + [core.STATUS_TEXT[status].format(h=h or 0.0)], warn=True)}
        self.rb_target.setToGeometry(polyline_geom(path), hit.layer)
        gap_pts = run_pts + path[::-1]
        area = core.polygon_area(gap_pts)
        if len(gap_pts) >= 3:
            gap = QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in gap_pts + gap_pts[:1]]])
            self.rb_gap.setToGeometry(gap, hit.layer)
            try:
                if not gap.isGeosValid():
                    area = gap.makeValid().area()
            except Exception:  # pylint: disable=broad-exception-caught
                pass           # keep the shoelace area
        else:
            self.rb_gap.reset(POLY)
        lines.append("Largest movement: {:.3f}{}  ·  area between: {:.4g}{}".format(
            h or 0.0, unit, area, unit + "²" if unit else ""))

        # the click itself, without writing
        collect = self._new_collect()
        self._run_align(state, k, hit.pi, hit.ri, ref, cfg, crs, hit.q, collect)
        keep = [p for pth in collect["paths"] for p in pth]
        ok, problem, _by_layer, changed = self._prepare(
            state, collect["primary"], keep, collect.get("eps", 0.0),
            collect.get("area_checks", True))
        if not ok:
            if problem == "Nothing to change." and collect["messages"]:
                problem = " ".join(dict.fromkeys(collect["messages"]))
            return {"ok": False, "html": _tip_html("Click would be refused",
                                                   lines + [problem], warn=True)}
        moved = ["'{}' feature {}".format(e.layer.name(), e.fid) for kk, e, _g in changed
                 if kk in collect["primary"]]
        along = ["'{}' feature {}".format(e.layer.name(), e.fid) for kk, e, _g in changed
                 if kk not in collect["primary"]]
        lines.insert(0, "Moves: " + ", ".join(moved))
        if along:
            lines.insert(1, "Moves along (coincident): " + _short_list(along))
        if collect["readonly"]:
            lines.append("Not changed (not in edit mode): "
                         + ", ".join("'{}'".format(n) for n in sorted(collect["readonly"])))
        return {"ok": True, "moved": moved, "along": along, "area": area, "h": h,
                "html": _tip_html("Click to align", lines)}

    def _show_tip(self, text):
        pt = self.toCanvasCoordinates(self._hover_pt) if self._hover_pt is not None else None
        self.info_box.show_text(text, pt)

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.controls.update_units()
        cfg = self.controls.current_settings()
        if cfg.tol <= cfg.eps:
            self._report("The alignment tolerance must be larger than the shared-edge "
                         "tolerance.", Qgis.MessageLevel.Warning)
            return
        alt = self._alt if self._hover_key is not None else 0
        self._clear_hover()
        try:
            if cfg.mode == self.controls.MODE_EDGE:
                self._align_clicked_edge(e.mapPoint(), cfg, alt)
            else:
                self._align_selected(e.mapPoint(), cfg)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            log_exception(Qgis.MessageLevel.Critical)
            self._report("Unexpected error; no changes were made. {}: {}  (Full details: "
                         "View > Panels > Log Messages, tab 'Edge Aligner'.)"
                         .format(type(ex).__name__, ex), Qgis.MessageLevel.Critical, 0)

    # ------------------------------------------------------------ helpers
    def _report(self, text, level=Qgis.MessageLevel.Info, duration=6):
        self.iface.messageBar().pushMessage(TITLE, text, level=level, duration=duration)
        self.controls.set_status(text)

    def _radius_for(self, layer, map_pt):
        # Click search radius: SEARCH_PIXELS screen pixels in canvas map units.
        r = SEARCH_PIXELS * self.canvas().mapUnitsPerPixel()
        if layer.crs() == self.canvas().mapSettings().destinationCrs():
            return r
        rect = QgsRectangle(map_pt.x() - r, map_pt.y() - r, map_pt.x() + r, map_pt.y() + r)
        lr = self.toLayerCoordinates(layer, rect)
        return max(lr.width(), lr.height()) / 2.0

    def _pick_target_segment(self, map_pt, layers, eps=1e-6):
        """Nearest edge of a feature in `layers`. Where edges coincide (within
        eps), a polygon edge wins over a line (lines lying on the boundary then
        move along as coincident features), then the polygon on the side of
        the click (the one containing the click point), then the active layer."""
        tie = max(eps, 1e-6)
        active = self.iface.activeLayer()
        active_id = active.id() if active is not None else None
        hits = []                                   # (d, hit, geometry, click point)
        for layer in layers:
            p = self.toLayerCoordinates(layer, map_pt)
            pt = (p.x(), p.y())
            r = self._radius_for(layer, map_pt)
            for f, g, parts in feature_parts(layer, rect_request(pt, r)):
                for pi, ri, ring in rings_of(parts):
                    res = core.nearest_segment(ring, pt, r)
                    if res is None:
                        continue
                    h = Hit()
                    h.layer, h.fid, h.pi, h.ri = layer, f.id(), pi, ri
                    h.d, h.k, h.q = res
                    hits.append((h, g, pt))
        if not hits:
            return None
        dmin = min(h.d for h, _g, _pt in hits)
        tied = [(h, g, pt) for h, g, pt in hits if h.d <= dmin + tie]
        if len(tied) == 1:
            return tied[0][0]

        def prio(item):
            h, g, pt = item
            poly = h.layer.geometryType() == POLY
            inside = poly and g.contains(QgsGeometry.fromPointXY(QgsPointXY(*pt)))
            return (0 if poly else 1, 0 if inside else 1,
                    0 if h.layer.id() == active_id else 1, h.d)
        return min(tied, key=prio)[0]

    def _req(self, layer, request):
        """Reference layers may use another CRS than the layers being edited:
        their geometries (and the filter rectangle) are then in the CRS of
        the edited layer (self._work_crs). References are never written, so
        reprojecting them cannot shift any stored vertex."""
        crs = self._work_crs
        if crs is not None and crs.isValid() and layer.crs() != crs:
            request.setDestinationCrs(crs, QgsProject.instance().transformContext())
        return request

    def _ref_geometry(self, layer, fid):
        req = self._req(layer, QgsFeatureRequest(fid).setNoAttributes())
        for f in layer.getFeatures(req):
            return f.geometry()
        return None

    def _line_chains(self, layer, bb, exclude_fids):
        geoms = []
        for f in layer.getFeatures(self._req(layer, bbox_request(bb))):
            if f.id() in exclude_fids:
                continue
            g = f.geometry()
            if not geometry_problem(g):
                geoms.append(g)
        if not geoms:
            return []
        merged = QgsGeometry.unaryUnion(geoms).mergeLines()
        if merged.isNull() or merged.isEmpty():
            return []
        lines = merged.asMultiPolyline() if merged.isMultipart() else [merged.asPolyline()]
        out = []
        for line in lines:
            pts = [(p.x(), p.y()) for p in line]
            if len(pts) >= 2:
                out.append((pts, pts[0] == pts[-1]))
        return out

    def _chain_keys(self, layer, bb, lw, eps):
        keys = set()
        for f, _g, parts in feature_parts(layer, self._req(layer, bbox_request(bb))):
            if all(lw.nearest(p, eps) is not None
                   for _pi, _ri, ring in rings_of(parts) for p in ring.pts):
                keys.add((layer.id(), f.id()))
        return keys

    def _finish_reference(self, choice, cands, cfg, chain_bb):
        """Build the Reference. Every candidate whose boundary coincides with
        the chosen one at the click position also stays fixed, so a polygon
        sharing the reference boundary is never dragged along."""
        keys = set()
        for c in cands:
            if c.d <= choice.d + cfg.eps and core.dist(c.pt, choice.pt) <= cfg.eps:
                keys |= (c.keys if c.keys is not None
                         else self._chain_keys(c.layer, chain_bb, c.lw, cfg.eps))
        return Reference(choice.lw, keys, choice.layer, choice.label, choice.m)

    def _candidates(self, ref_layers, pt, radius, exclude, cfg, chain_bb, seg=None,
                    click_geom=None):
        """Reference candidates within `radius` of `pt`: rings of reference
        polygons and merged chains of reference lines. Features whose key is
        in `exclude` are skipped; with seg=(a, b), boundaries that already
        contain that segment are skipped too; with `click_geom`, polygon
        candidates record whether they contain the click."""
        cands = []
        cell = 2.0 * cfg.tol
        for layer in ref_layers:
            if layer.geometryType() == POLY:
                for f, g, parts in feature_parts(
                        layer, self._req(layer, rect_request(pt, radius)), exclude):
                    key = (layer.id(), f.id())
                    inside = click_geom is not None and g.contains(click_geom)
                    label = "'{}' feature {}".format(layer.name(), f.id())
                    for _pi, _ri, ring in rings_of(parts):
                        lw = core.Linework(ring.to_list(), True, cell)
                        if seg is not None and core.contains_segment(lw, seg[0], seg[1],
                                                                     cfg.eps):
                            continue
                        r = lw.nearest(pt, radius)
                        if r:
                            cands.append(Cand(r, lw, {key}, layer, label, inside))
            else:
                excl = {fid for lid, fid in exclude if lid == layer.id()}
                for pts, closed in self._line_chains(layer, chain_bb, excl):
                    lw = core.Linework(pts, closed, cell)
                    if seg is not None and core.contains_segment(lw, seg[0], seg[1], cfg.eps):
                        continue
                    r = lw.nearest(pt, radius)
                    if r:
                        cands.append(Cand(r, lw, None, layer,
                                          "line(s) of '{}'".format(layer.name())))
        return cands

    def _reference_for_edge(self, ref_layers, primary_key, q, a, b, cfg, chain_bb,
                            click_pt=None, alt=0):
        """Nearest reference boundary to q that does not already contain the
        clicked segment a-b. With 'prefer other layer', candidates from
        layers other than the clicked feature's layer win. Of coincident
        candidates, the polygon containing `click_pt` wins.
        alt > 0 (Tab): the alt-th other candidate instead, in order of
        distance (cycling)."""
        cands = self._candidates(ref_layers, q, cfg.tol, {primary_key}, cfg, chain_bb,
                                 seg=(a, b))
        if not cands:
            return None
        pool = cands
        if cfg.prefer_other:
            other = [c for c in cands if c.layer.id() != primary_key[0]]
            if other:
                pool = other
        choice = min(pool, key=lambda c: c.d)
        if click_pt is not None:
            choice = self._prefer_containing(choice, pool, click_pt, cfg)
        # candidates the user can cycle through with Tab: the default choice
        # first, then the others by distance; boundaries coinciding with one
        # already listed at the nearest point are the same choice
        order = [choice]
        for c in sorted(cands, key=lambda c: c.d):
            if not any(c is o or (abs(c.d - o.d) <= cfg.eps and core.dist(c.pt, o.pt) <= cfg.eps)
                       for o in order):
                order.append(c)
        pick = order[alt % len(order)]
        ref = self._finish_reference(pick, cands, cfg, chain_bb)
        ref.alt, ref.alts = alt % len(order), len(order)
        return ref

    def _prefer_containing(self, choice, pool, click_pt, cfg):
        """Where several reference boundaries coincide at the nearest point
        (e.g. two polygons sharing an edge), take the polygon that contains
        the click point: its own vertices then bound the stretch."""
        tied = [c for c in pool if c.d <= choice.d + cfg.eps
                and core.dist(c.pt, choice.pt) <= cfg.eps]
        if len(tied) < 2:
            return choice
        click_geom = QgsGeometry.fromPointXY(QgsPointXY(*click_pt))
        for c in tied:
            if c.keys and c.layer.geometryType() == POLY:
                _lid, fid = next(iter(c.keys))
                g = self._ref_geometry(c.layer, fid)
                if not geometry_problem(g) and straight(g).contains(click_geom):
                    return c
        return choice

    def _junction_variants(self, ref, primary_key, bb, cfg, q):
        """The reference plus alternatives that continue through junctions
        (nodes where three or more lines meet) onto the connecting lines."""
        if ref.layer.geometryType() != LINE or ref.lw.closed:
            return [ref]
        excl = {primary_key[1]} if primary_key[0] == ref.layer.id() else set()
        chains = [pts for pts, closed in self._line_chains(ref.layer, bb, excl)
                  if not closed]
        base = list(ref.lw.pts)
        eps = cfg.eps

        def connectors(node):
            out = []
            for pts in chains:
                if pts == base or pts[::-1] == base:
                    continue
                if core.dist(pts[0], node) <= eps:
                    out.append(list(pts))
                elif core.dist(pts[-1], node) <= eps:
                    out.append(list(reversed(pts)))
            return out

        starts = [None] + connectors(base[0])
        ends = [None] + connectors(base[-1])
        out = [ref]
        for s in starts:
            for e in ends:
                if s is None and e is None:
                    continue
                if len(out) > 12:
                    return out
                pts = (list(reversed(s))[:-1] if s else []) + base + (e[1:] if e else [])
                lw = core.Linework(pts, False, 2.0 * cfg.tol)
                near = lw.nearest(q, max(cfg.tol * 3.0, eps))
                keys = set(ref.keys or ()) | self._chain_keys(ref.layer, bb, lw, eps)
                out.append(Reference(lw, keys, ref.layer, ref.label,
                                     near[1] if near else ref.m))
        return out

    def _best_variant(self, ref, ring, hit, key, cfg):
        """Among the reference and its continuations through junctions, take
        the one that allows the longest aligned stretch within the tolerance
        (the original reference wins ties)."""
        alts = self._junction_variants(ref, key, ring.bbox(cfg.tol), cfg, hit.q)
        if len(alts) < 2:
            return ref
        best, best_len = ref, -1.0
        for alt in alts:
            res = core.align_ring(ring.copy(), alt.lw, cfg.tol, cfg.eps, click_point=hit.q)
            if res.ring is None or not res.paths:
                continue
            length = sum(core.polyline_cum(p)[-1] for p in res.paths)
            if length > best_len + cfg.eps:
                best, best_len = alt, length
        return best

    def _network_reference(self, ref_layers, primary_key, ring, hit, a, b, cfg, ref):
        """The boundaries of all reference features near the stretch,
        combined into one network, so the edge can follow the reference
        across several features (e.g. along one polygon to a corner, then
        along the next one). Used instead of the single reference when that
        cannot align the stretch, or when another reference boundary meets
        the edge closer to the click (the stretch then ends there)."""
        tol, eps = cfg.tol, cfg.eps
        st, _m = core.click_stretch(ring.copy(), ref.lw, tol, eps, hit.q)
        area = [a, b]
        if st is not None:
            work = ring.copy()
            run = core.locate_run(work, core.anchor_at(work, st[0], eps),
                                  core.anchor_at(work, st[1], eps))
            if run is not None:
                rp = work.run_points(*run)
                if core.build_path(ref.lw, rp, tol, eps)[1] == "aligned":
                    return None         # already lies on the nearest reference
                area += rp
        bb = core.bbox_of(area, tol + eps)
        feats = []
        for layer in ref_layers:
            for f, _g, parts in feature_parts(layer, self._req(layer, bbox_request(bb)),
                                              {primary_key}):
                rings = [r.to_list() for _pi, _ri, r in rings_of(parts)]
                pieces = [pc for r in rings for pc in core.clip_polyline(r, bb)]
                if pieces:
                    feats.append(((layer.id(), f.id()), layer.name(), rings, pieces))
        # Features sharing the stretch move along (or stay as ties), so they
        # are not used as references here. The stretch depends on the
        # network, so repeat until no used feature shares it.
        segs = [(a, b)]
        excluded = set()
        net = stn = None
        for _round in range(4):
            for key, _name, rings, _pc in feats:
                if key not in excluded and any(
                        core.polyline_has_segment(r, p, q, eps)
                        for r in rings for p, q in segs if core.dist(p, q) > eps):
                    excluded.add(key)
            used = [ft for ft in feats if ft[0] not in excluded]
            if len(used) < 2 or sum(len(l) for ft in used for l in ft[3]) > MAX_NETWORK_VERTICES:
                return None
            net = core.Network([l for ft in used for l in ft[3]], 2.0 * tol, eps)
            stn, _m = core.click_stretch(ring.copy(), net, tol, eps, hit.q)
            if stn is None:
                return None
            work = ring.copy()
            run = core.locate_run(work, core.anchor_at(work, stn[0], eps),
                                  core.anchor_at(work, stn[1], eps))
            if run is None:
                return None
            rp = work.run_points(*run)
            new_segs = [(rp[i], rp[i + 1]) for i in range(len(rp) - 1)]
            if not any(core.polyline_has_segment(r, p, q, eps)
                       for ft in used for r in ft[2] for p, q in new_segs
                       if core.dist(p, q) > eps):
                break
            segs = segs + new_segs
        else:
            return None
        keys = set(ft[0] for ft in used)
        names = list(dict.fromkeys(ft[1] for ft in used))
        res = core.align_ring(ring.copy(), net, tol, eps, click_point=hit.q)
        if res.ring is None:
            return None
        before = set(core.fold_backs(ring.to_list(), ring.closed, eps))
        if any(p not in before for p in core.fold_backs(res.ring.to_list(), ring.closed, eps)):
            return None                 # the route would double back on the edge
        if ring.closed and ring_valid(ring.to_list()) and not ring_valid(res.ring.to_list()):
            return None                 # keep the single reference
        if st is not None and core.align_ring(ring.copy(), ref.lw, tol, eps,
                                              click_point=hit.q).ring is not None:
            if not (stn[0] > st[0] + eps or stn[1] < st[1] - eps):
                return None             # the single reference does the same job
        near = net.nearest(hit.q, tol + eps)
        label = "the combined reference boundaries of " + ", ".join(
            "'{}'".format(n) for n in names)
        return Reference(net, keys | (set(ref.keys or ()) - excluded), ref.layer, label,
                         near[1] if near else 0.0)

    def _reference_at_point(self, ref_layers, pt, radius, exclude, cfg, chain_bb):
        """Reference boundary nearest to the click (selected-features mode).
        On ties, a polygon containing the click point wins."""
        click_geom = QgsGeometry.fromPointXY(QgsPointXY(*pt))
        cands = self._candidates(ref_layers, pt, radius, exclude, cfg, chain_bb,
                                 click_geom=click_geom)
        if not cands:
            return None
        dmin = min(c.d for c in cands)
        tied = [c for c in cands if c.d <= dmin + max(cfg.eps, radius * 0.05)]
        choice = next((c for c in tied if c.inside), min(tied, key=lambda c: c.d))
        return self._finish_reference(choice, cands, cfg, chain_bb)

    def _make_provider(self, state, crs, exclude):
        """Returns provider(bbox) -> new NeighbourItems (rings of other
        features in that box). Layers in edit mode give working copies;
        other layers are read-only (only reported)."""
        seen = set()
        layers = [l for l in project_layers() if l.crs() == crs]

        def provider(bb):
            items = []
            for layer in layers:
                editable = layer.isEditable()
                for f in layer.getFeatures(bbox_request(bb)):
                    key = (layer.id(), f.id())
                    if key in exclude:
                        continue
                    if editable:
                        entry = state.get(layer, f.id(), f)
                        if entry is None:
                            continue
                        parts = entry.parts
                    else:
                        g = f.geometry()
                        if geometry_problem(g):
                            continue
                        parts = geom_to_parts(g)[1]
                    for pi, ri, ring in rings_of(parts):
                        ck = (key, pi, ri)
                        if ck in seen or not core.bbox_intersects(ring.bbox(), bb):
                            continue
                        seen.add(ck)
                        items.append(core.NeighbourItem(ring.copy(), not editable,
                                                        (key, layer, pi, ri)))
            return items
        return provider

    def _run_align(self, state, key, pi, ri, ref, cfg, crs, click_q, collect):
        entry = state.entries[key]
        ring = entry.parts[pi][ri]
        provider = (self._make_provider(state, crs, {key} | ref.keys)
                    if cfg.neighbours else None)
        res = core.align_ring(ring, ref.lw, cfg.tol, cfg.eps, click_point=click_q,
                              neighbour_provider=provider,
                              hole=entry.kind == "poly" and ri > 0)
        collect["primary"].add(key)
        collect["ref"] = ref
        collect["eps"] = cfg.eps
        collect["area_checks"] = bool(cfg.neighbours)
        collect["messages"].extend(res.messages)
        for it in res.readonly_hits:
            collect["readonly"].add(it.payload[1].name())
        if res.ring is None:
            return
        entry.parts[pi][ri] = res.ring
        state.modified.add(key)
        for it, new_ring in res.neighbours:
            nkey, _layer, npi, nri = it.payload
            state.entries[nkey].parts[npi][nri] = new_ring
            state.modified.add(nkey)
        collect["paths"].extend(res.paths)

    def _commit(self, state, label, primary_keys=(), keep_pts=(), eps=0.0,
                area_checks=True):
        """Validate all changes, then write them (one edit command per layer).
        Other features whose shape is unchanged (only vertices inserted away
        from the new edges in keep_pts, or a ring starting at another vertex)
        are not written. With area_checks, polygons must not get new overlaps
        or new enclosed gaps with other polygons of their layer.
        All or nothing: if writing fails in any layer (or raises an error),
        the layers already written are taken back.
        Returns (ok, message)."""
        ok, problem, by_layer, changed = self._prepare(state, primary_keys, keep_pts, eps,
                                                       area_checks)
        if not ok:
            return False, problem
        for layer, _items in by_layer.values():
            if not layer.isEditable():
                return False, ("'{}' is no longer in edit mode; no changes were made."
                               .format(layer.name()))
        for _key, e, _g in changed:
            cur = None
            for f in e.layer.getFeatures(QgsFeatureRequest(e.fid).setNoAttributes()):
                cur = f.geometry()
            if cur is None or cur.asWkb() != e.orig_geom.asWkb():
                return False, ("Feature {} in '{}' was changed or deleted in the meantime; no "
                               "changes were made. Try again.".format(e.fid, e.layer.name()))
        counts = []
        written = []

        def take_back():
            # all or nothing: take back the layers already written, so that
            # boundaries shared between layers stay identical
            for done in reversed(written):
                done.undoStack().undo()
                done.triggerRepaint()

        for layer, items in by_layer.values():
            layer.beginEditCommand(label)
            try:
                ok = all(layer.changeGeometry(fid, g) for fid, g in items)
            except Exception as ex:     # pylint: disable=broad-exception-caught
                log_exception(Qgis.MessageLevel.Critical)
                layer.destroyEditCommand()
                take_back()
                return False, ("Writing to '{}' failed ({}: {}); no changes were made (changes "
                               "already written to other layers were taken back)."
                               .format(layer.name(), type(ex).__name__, ex))
            if not ok:
                layer.destroyEditCommand()
                take_back()
                return False, ("Writing to '{}' failed; no changes were made (changes already "
                               "written to other layers were taken back).".format(layer.name()))
            layer.endEditCommand()
            written.append(layer)
            counts.append("{}: {}".format(layer.name(), len(items)))
        for layer in written:
            layer.triggerRepaint()
        return True, ", ".join(counts)

    def _prepare(self, state, primary_keys=(), keep_pts=(), eps=0.0, area_checks=True):
        """The checks of _commit, without writing (also used by the preview).
        Returns (ok, message, by_layer, changed)."""
        by_layer = {}
        changed = []
        # fixed order: the clicked feature(s) first, so messages are reproducible
        for key in sorted(state.modified, key=lambda k: (k not in primary_keys, k)):
            e = state.entries[key]
            cur = [[r.to_list() for r in part] for part in e.parts]
            if cur == e.orig_lists:
                continue
            if key not in primary_keys and only_densified(e.kind, e.orig_lists, cur,
                                                          keep_pts, eps):
                continue
            g = e.new_geometry()
            broken = g is None or g.isEmpty() or (e.orig_valid and not g.isGeosValid())
            if broken:
                if key in primary_keys:
                    role = "the feature you clicked"
                    hint = "Try a smaller tolerance."
                else:
                    role = "a coincident feature that would move along"
                    hint = ("To move only the clicked feature, untick 'Move coincident "
                            "features'.")
                return False, ("Aborted: feature {} in '{}' ({}) would become an invalid "
                               "geometry (e.g. a sliver collapsing or a self-intersection). "
                               "No changes were made. {}"
                               .format(e.fid, e.layer.name(), role, hint)), by_layer, changed
            by_layer.setdefault(e.layer.id(), (e.layer, []))[1].append((e.fid, g))
            changed.append((key, e, g))
        if not by_layer:
            return False, "Nothing to change.", by_layer, changed
        problem = self._topology_problem(changed, primary_keys, eps, area_checks)
        if problem:
            return False, problem, by_layer, changed
        return True, None, by_layer, changed

    @staticmethod
    def _role(key, primary_keys):
        return ("the feature you clicked" if key in primary_keys
                else "a coincident feature that would move along")

    def _topology_problem(self, changed, primary_keys, eps, area_checks):
        """Problems GEOS validity does not catch. Returns a message or None.
        - fold-back: a boundary turning back onto itself (zero-width spike);
        - (area_checks) a polygon getting a new overlap with another polygon
          of its layer, or a new enclosed gap (a hole between polygons of the
          layer that does not grow an existing one)."""
        e_tol = max(eps, 1e-9)
        for key, e, _g in changed:
            closed = e.kind == "poly"
            before = set(p for part in e.orig_lists for r in part
                         for p in core.fold_backs(r, closed, e_tol))
            for part in e.parts:
                for r in part:
                    for p in core.fold_backs(r.to_list(), closed, e_tol):
                        if p not in before:
                            return ("Aborted: feature {} in '{}' ({}) would fold back onto "
                                    "its own boundary at ({:.3f}, {:.3f}) (a spike of zero "
                                    "width). No changes were made. Try a smaller tolerance "
                                    "or click closer to the part you want to align."
                                    .format(e.fid, e.layer.name(),
                                            self._role(key, primary_keys), p[0], p[1]))
        if not area_checks:
            return None
        tiny = max(10.0 * eps * eps, 1e-10)
        layers = {}
        for key, e, g in changed:
            if e.kind == "poly":
                layers.setdefault(e.layer.id(), (e.layer, []))[1].append((key, e, g))
        for layer, items in layers.values():
            try:
                msg = self._area_problem(layer, items, primary_keys, eps, tiny)
            except Exception:           # pylint: disable=broad-exception-caught
                log_exception(Qgis.MessageLevel.Warning)    # e.g. GEOS failing on invalid input data
                msg = None
            if msg:
                return msg
        return None

    def _area_problem(self, layer, items, primary_keys, eps, tiny):
        bb = None
        for _key, e, g in items:
            d = e.orig_geom.symDifference(g)
            if d is None or d.isEmpty():
                continue
            r = d.boundingBox()
            bb = core.bbox_union(bb, (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()))
        if bb is None:
            return None
        pad = 0.05 * max(bb[2] - bb[0], bb[3] - bb[1]) + 10.0 * eps
        bb = (bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad)
        rect = QgsGeometry.fromRect(QgsRectangle(*bb))
        frame = polyline_geom([(bb[0], bb[1]), (bb[2], bb[1]), (bb[2], bb[3]),
                               (bb[0], bb[3]), (bb[0], bb[1])])
        keys = {key for key, _e, _g in items}
        old = [(key, e.fid, e.orig_geom.intersection(rect)) for key, e, _g in items]
        new = [(key, e.fid, g.intersection(rect)) for key, e, g in items]
        others = []
        for f in layer.getFeatures(bbox_request(bb)):
            if (layer.id(), f.id()) in keys:
                continue
            og = f.geometry()
            if geometry_problem(og):
                continue
            others.append((f.id(), og.intersection(rect)))
        # new overlaps
        for i, (key, fid, ng) in enumerate(new):
            og_ = old[i][2]
            pairs = [(ofid, og, og) for ofid, og in others]
            pairs += [(new[j][1], new[j][2], old[j][2]) for j in range(i + 1, len(new))]
            for ofid, o_new, o_old in pairs:
                grown = ng.intersection(o_new).area() - og_.intersection(o_old).area()
                if grown > tiny:
                    return ("Aborted: feature {} in '{}' ({}) would overlap feature {} of "
                            "the same layer ({:.4g} square map units). No changes were made."
                            .format(fid, layer.name(), self._role(key, primary_keys), ofid,
                                    grown))
        # new enclosed gaps
        rest = [g for _fid, g in others]
        gap_before = rect.difference(QgsGeometry.unaryUnion([g for _k, _f, g in old] + rest))
        gap_after = rect.difference(QgsGeometry.unaryUnion([g for _k, _f, g in new] + rest))
        if gap_after.isEmpty():
            return None
        pieces = (gap_after.asGeometryCollection() if gap_after.isMultipart()
                  else [gap_after])
        for piece in pieces:
            if piece.area() <= tiny or piece.intersects(frame):
                continue
            if not gap_before.isEmpty() and piece.intersection(gap_before).area() > tiny:
                continue                # an existing gap that changed shape
            pt = piece.pointOnSurface().asPoint()
            fids = ", ".join(str(fid) for _k, fid, _g in new)
            return ("Aborted: the alignment would leave a new gap of {:.4g} square map units "
                    "near ({:.3f}, {:.3f}) between feature {} in '{}' and its neighbours. "
                    "No changes were made.".format(piece.area(), pt.x(), pt.y(), fids,
                                                   layer.name()))
        return None

    def _flash_reference(self, ref, crs):
        """Flash the part of the reference near the click in orange."""
        try:
            half = 300.0 * self.canvas().mapUnitsPerPixel()
            if isinstance(ref.lw, core.Network):
                lines = ref.lw.lines_near(ref.lw.point_at(ref.m), half)
            else:
                lines = [ref.lw.substring(max(0.0, ref.m - half),
                                          min(ref.lw.length, ref.m + half))]
            geoms = [polyline_geom(pts) for pts in lines if len(pts) > 1]
            if geoms:
                self.canvas().flashGeometries(
                    geoms, crs, QColor(255, 140, 0, 255), QColor(255, 140, 0, 0), 3, 500)
        except Exception:               # pylint: disable=broad-exception-caught
            log_exception()             # the flash is only a visual hint

    def _finish(self, state, collect, crs):
        keep = [p for path in collect["paths"] for p in path]
        ok, info = self._commit(state, "Align edge", collect["primary"], keep,
                                collect.get("eps", 0.0), collect.get("area_checks", True))
        msgs = list(dict.fromkeys(collect["messages"]))
        ref = collect.get("ref")
        ref_text = " Reference used: {} (flashed orange).".format(ref.label) if ref else ""
        if not ok:
            detail = info if info != "Nothing to change." or not msgs else " ".join(msgs)
            self._report(detail + ref_text, Qgis.MessageLevel.Warning)
            if ref is not None:
                self._flash_reference(ref, crs)
            return
        text = "Aligned to " + (ref.label if ref else "reference") + ". Features changed - " \
               + info + "."
        level = Qgis.MessageLevel.Success
        if collect["readonly"]:
            text += (" Coincident features in layers NOT changed (not in edit mode): "
                     + ", ".join(sorted(collect["readonly"])) + ".")
            level = Qgis.MessageLevel.Warning
        if msgs and collect.get("multi"):
            text += " Notes: " + " ".join(msgs)
        self._report(text, level)
        try:
            geoms = [polyline_geom(path) for path in collect["paths"] if len(path) > 1]
            if geoms:
                self.canvas().flashGeometries(geoms, crs)
        except Exception:               # pylint: disable=broad-exception-caught
            log_exception()             # the flash is only a visual hint; the edit is written

    @staticmethod
    def _new_collect(multi=False):
        return {"messages": [], "readonly": set(), "paths": [], "multi": multi,
                "primary": set(), "ref": None}

    # ------------------------------------------------------------ modes
    def _resolve_clicked(self, map_pt, cfg, alt=0):
        """Clicked feature and reference for clicked-edge mode. alt: candidate
        reference chosen with Tab (0 = the default choice).
        Returns ((hit, state, key, entry, ring, ref, crs), None) or
        (None, (message, level))."""
        warn = Qgis.MessageLevel.Warning
        targets = editable_layers()
        if not targets:
            return None, ("No line or polygon layer is in edit mode. Toggle editing on "
                          "the layer(s) you want to change.", warn)
        if not cfg.ref_layers:
            return None, ("Tick at least one reference layer under ▾ > References on the Edge Aligner button.", warn)
        hit = self._pick_target_segment(map_pt, targets, cfg.eps)
        if hit is None:
            return None, ("No edge of a feature in a layer being edited was found under "
                          "the cursor.", warn)
        layer = hit.layer
        crs = layer.crs()
        self._work_crs = crs            # references in other CRSs are reprojected to it
        ref_layers = list(cfg.ref_layers)
        state = WorkState()
        key = (layer.id(), hit.fid)
        entry = state.get(layer, hit.fid)
        if entry is None:
            return None, ("The clicked feature has an unsupported geometry (empty, or with "
                          "arcs).", warn)
        ring = entry.parts[hit.pi][hit.ri]
        a, b = ring.seg(hit.k)
        p = self.toLayerCoordinates(layer, map_pt)
        ref = self._reference_for_edge(ref_layers, key, hit.q, a, b, cfg,
                                       ring.bbox(cfg.tol), click_pt=(p.x(), p.y()), alt=alt)
        if ref is None:
            return None, ("No reference boundary within {:g} map units of the clicked edge. "
                          "(Boundaries that already share the clicked edge are ignored.)"
                          .format(cfg.tol), warn)
        if getattr(cfg, "through_junctions", False):
            chosen = ref
            ref = self._best_variant(ref, ring, hit, key, cfg)
            if chosen.alt == 0:         # a reference picked with Tab is used as it is
                combined = self._network_reference(ref_layers, key, ring, hit, a, b, cfg, ref)
                if combined is not None:
                    ref = combined
            ref.alt, ref.alts = chosen.alt, chosen.alts
        return (hit, state, key, entry, ring, ref, crs), None

    def _align_clicked_edge(self, map_pt, cfg, alt=0):
        res, msg = self._resolve_clicked(map_pt, cfg, alt)
        if res is None:
            self._report(msg[0], msg[1])
            return
        hit, state, key, _entry, _ring, ref, crs = res
        collect = self._new_collect()
        self._run_align(state, key, hit.pi, hit.ri, ref, cfg, crs, hit.q, collect)
        self._finish(state, collect, crs)

    def _align_selected(self, map_pt, cfg):
        targets = [l for l in editable_layers() if l.selectedFeatureCount() > 0]
        if not targets:
            self._report("Select the features to align in a layer that is in edit mode, "
                         "then click the reference boundary.", Qgis.MessageLevel.Warning)
            return
        if not cfg.ref_layers:
            self._report("Tick at least one reference layer under ▾ > References on the Edge Aligner button.",
                         Qgis.MessageLevel.Warning)
            return
        crs = targets[0].crs()
        if any(l.crs() != crs for l in targets):
            self._report("The layers with selected features use different CRSs.",
                         Qgis.MessageLevel.Warning)
            return
        self._work_crs = crs            # references in other CRSs are reprojected to it
        ref_layers = list(cfg.ref_layers)
        p = self.toLayerCoordinates(targets[0], map_pt)
        pt = (p.x(), p.y())
        radius = self._radius_for(targets[0], map_pt)

        state = WorkState()
        selected = []
        bb = None
        skipped = 0
        for layer in targets:
            for fid in layer.selectedFeatureIds():
                e = state.get(layer, fid)
                if e is None:
                    skipped += 1
                    continue
                selected.append((layer.id(), fid))
                for _pi, _ri, ring in rings_of(e.parts):
                    bb = core.bbox_union(bb, ring.bbox(cfg.tol))
        if not selected:
            self._report("None of the selected features has a supported geometry.",
                         Qgis.MessageLevel.Warning)
            return
        ref = self._reference_at_point(ref_layers, pt, radius, set(selected), cfg, bb)
        if ref is None:
            self._report("No reference boundary at the click position. Click on the "
                         "boundary of a feature in a reference layer.",
                         Qgis.MessageLevel.Warning)
            return
        ref_bb = ref.lw.bbox(cfg.tol)
        collect = self._new_collect(multi=True)
        for key in selected:
            e = state.entries[key]
            for pi in range(len(e.parts)):
                for ri in range(len(e.parts[pi])):
                    if core.bbox_intersects(e.parts[pi][ri].bbox(), ref_bb):
                        self._run_align(state, key, pi, ri, ref, cfg, crs, None, collect)
        if skipped:
            collect["messages"].append("{} selected feature(s) skipped (unsupported "
                                       "geometry).".format(skipped))
        self._finish(state, collect, crs)

    # ------------------------------------------------------------ batch
    def _batch_references(self, base_layer, bb, cfg, exclude=()):
        """Reference boundaries of the base layer in the box bb (work CRS):
        every polygon ring, or the merged line chains."""
        refs = []
        cell = 2.0 * cfg.tol
        if base_layer.geometryType() == POLY:
            req = self._req(base_layer, bbox_request(bb))
            for f, _g, parts in feature_parts(base_layer, req, exclude):
                label = "'{}' feature {}".format(base_layer.name(), f.id())
                for _pi, _ri, ring in rings_of(parts):
                    lw = core.Linework(ring.to_list(), True, cell)
                    refs.append(Reference(lw, {(base_layer.id(), f.id())}, base_layer,
                                          label, 0.0))
        else:
            excl = {fid for lid, fid in exclude if lid == base_layer.id()}
            for pts, closed in self._line_chains(base_layer, bb, excl):
                lw = core.Linework(pts, closed, cell)
                refs.append(Reference(lw, self._chain_keys(base_layer, bb, lw, cfg.eps),
                                      base_layer, "line(s) of '{}'".format(base_layer.name()),
                                      0.0))
        return refs

    def batch_scan(self, base_layer, layers, cfg, extent=None, progress=None):
        """Every feature of `layers` with a stretch that can be aligned to a
        boundary of `base_layer` within cfg.tol (like selected-features mode,
        but for all features). extent: QgsRectangle in map canvas coordinates
        (None = whole layers). progress(done, total) -> False cancels.
        Nothing is written. Returns [BatchItem]."""
        items = []
        jobs = []
        for layer in layers:
            req = QgsFeatureRequest().setNoAttributes()
            if extent is not None:
                req.setFilterRect(self.toLayerCoordinates(layer, extent))
            jobs.extend((layer, f) for f in layer.getFeatures(req))
        for n, (layer, f) in enumerate(jobs):
            if progress is not None and progress(n, len(jobs)) is False:
                break
            g = f.geometry()
            if geometry_problem(g):
                continue
            self._work_crs = layer.crs()
            key = (layer.id(), f.id())
            kind, parts = geom_to_parts(g)
            bb = None
            for _pi, _ri, ring in rings_of(parts):
                bb = core.bbox_union(bb, ring.bbox(cfg.tol))
            if bb is None:
                continue
            for ref in self._batch_references(base_layer, bb, cfg, {key}):
                ref_bb = ref.lw.bbox(cfg.tol)
                count, h = 0, 0.0
                for _pi, ri, ring in rings_of(parts):
                    if not core.bbox_intersects(ring.bbox(), ref_bb):
                        continue
                    res = core.align_ring(ring.copy(), ref.lw, cfg.tol, cfg.eps,
                                          hole=kind == "poly" and ri > 0)
                    if res.ring is None or not res.paths:
                        continue
                    count += len(res.paths)
                    h = max(h, core.hausdorff(ring.to_list(), res.ring.to_list()))
                if count:
                    items.append(BatchItem(layer, f.id(), ref, count, h))
        if progress is not None:
            progress(len(jobs), len(jobs))
        return items

    def batch_apply(self, item, cfg):
        """Align one scanned item, exactly like selected-features mode with
        that feature selected and that reference clicked: read again from the
        layer, same checks, one undoable edit command per changed layer.
        Returns (ok, message)."""
        layer = item.layer
        if not layer.isEditable():
            return False, "'{}' is not in edit mode.".format(layer.name())
        self._work_crs = layer.crs()
        state = WorkState()
        key = (layer.id(), item.fid)
        e = state.get(layer, item.fid)
        if e is None:
            return False, "Feature deleted or with an unsupported geometry."
        collect = self._new_collect(multi=True)
        ref_bb = item.ref.lw.bbox(cfg.tol)
        for pi in range(len(e.parts)):
            for ri in range(len(e.parts[pi])):
                if core.bbox_intersects(e.parts[pi][ri].bbox(), ref_bb):
                    self._run_align(state, key, pi, ri, item.ref, cfg, layer.crs(), None,
                                    collect)
        keep = [p for path in collect["paths"] for p in path]
        ok, info = self._commit(state, "Align edge", collect["primary"], keep, cfg.eps,
                                bool(cfg.neighbours))
        if not ok:
            if info == "Nothing to change." and collect["messages"]:
                info = " ".join(dict.fromkeys(collect["messages"]))
            return False, info
        if collect["readonly"]:
            info += ("; coincident features NOT changed (not in edit mode): "
                     + ", ".join(sorted(collect["readonly"])))
        return True, info


class BatchItem:
    """One feature that the batch alignment would change, with the base
    layer boundary it is aligned to."""
    __slots__ = ("layer", "fid", "ref", "count", "h")

    def __init__(self, layer, fid, ref, count, h):
        self.layer = layer
        self.fid = fid
        self.ref = ref          # Reference (a boundary of the base layer)
        self.count = count      # number of stretches that move
        self.h = h              # largest movement
