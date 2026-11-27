# -*- coding: utf-8 -*-
"""Edge Aligner toolbar: a single tool button with a drop-down arrow (like the
Vertex Tool button). Clicking the button toggles the map tool; the arrow opens
the settings: mode, reference layers, tolerance, options and help."""

from types import SimpleNamespace

from qgis.core import QgsApplication, QgsProject, QgsSettings, QgsUnitTypes
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

try:  # Qt6
    from qgis.PyQt.QtGui import QAction, QActionGroup
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction, QActionGroup

from .align_tool import LINE, editable_layers, is_supported_layer, project_layers

SETTINGS = "EdgeAligner/"
PROJECT_SCOPE = "EdgeAligner"
PROJECT_KEY = "reference_layers"

HINT_EDGE = ("Clicked edge: click the edge you want to MOVE (a feature in a layer in edit "
             "mode). The part between the nearest points where it cuts the reference is "
             "moved onto the reference. Tick both line and polygon layers as references to "
             "align in both directions. Where two boundaries coincide, click slightly inside "
             "the polygon you mean.")
HINT_SELECTED = ("Selected features: select features in a layer that is in edit mode, then "
                 "click the reference boundary. If two reference boundaries coincide there, "
                 "click slightly inside the reference polygon you mean.")
HINT_GENERAL = ("Hovering shows a preview: the stretch that will move (solid) and where it "
                "goes (dashed). Esc clears it. Features of other layers that share the moved "
                "stretch move with it if their layer is in edit mode. Reference layers are "
                "never changed. Every alignment is one undoable edit per layer.")

# (action attribute, settings key, label, tooltip); all default to on
OPTIONS = (
    ("act_other", "prefer_other_layer", "Prefer a reference from another layer",
     "Clicked edge mode: if a reference from another ticked layer is within the tolerance, "
     "it wins over the clicked feature's own layer. With lines and polygons both ticked, "
     "clicking a polygon edge aligns it to the line, clicking a line aligns it to the "
     "polygon boundary."),
    ("act_junc", "through_junctions", "Follow references through junctions",
     "Clicked edge mode: where a reference line meets other lines at a junction, the "
     "continuation that allows the longest aligned stretch within the tolerance is used. "
     "Where the edge runs along the boundaries of several reference features (e.g. along "
     "one polygon to a corner, then along another), the boundaries are combined and the "
     "edge follows them; the stretch then ends where another reference boundary meets it."),
    ("act_nbr", "move_neighbours", "Move coincident features of other layers",
     "Features whose boundary shares the moved stretch move with it, so no gaps or overlaps "
     "are created. Only layers in edit mode are changed."),
)


def _to_bool(v, default):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def _to_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _widget_action(menu, widget):
    act = QWidgetAction(menu)
    act.setDefaultWidget(widget)
    menu.addAction(act)
    return act


class AlignEdgeToolbar(QToolBar):
    MODE_EDGE = 0
    MODE_SELECTED = 1

    def __init__(self, iface, tool_action, parent=None):
        super().__init__("Edge Aligner", parent)
        self.setObjectName("EdgeAlignerToolbar")
        self.setToolTip("Edge Aligner")
        self.iface = iface
        self.last_status = ""

        # ---- one tool button: click = toggle the map tool, arrow = all settings
        self.btn_tool = QToolButton()
        self.btn_tool.setDefaultAction(tool_action)
        self.btn_tool.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.menu = QMenu(self.btn_tool)
        self.menu.setToolTipsVisible(True)
        self.btn_tool.setMenu(self.menu)
        self.addWidget(self.btn_tool)

        # ---- mode
        self.menu_mode = self.menu.addMenu("Mode")
        self.menu_mode.setToolTipsVisible(True)
        group = QActionGroup(self)
        group.setExclusive(True)
        self.act_edge = QAction("Clicked edge", self)
        self.act_sel = QAction("Selected features → clicked reference", self)
        for act, hint in ((self.act_edge, HINT_EDGE), (self.act_sel, HINT_SELECTED)):
            act.setCheckable(True)
            act.setToolTip(hint)
            act.setStatusTip(hint)
            group.addAction(act)
            self.menu_mode.addAction(act)

        # ---- reference layers
        self.menu_refs = self.menu.addMenu("References")
        self.menu_refs.setToolTip("Reference layers: boundaries the edges are aligned to "
                                  "(never changed)")
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(6, 6, 6, 6)
        head = QLabel("Reference layers (never changed)")
        bl.addWidget(head)
        self.lst = QListWidget()
        self.lst.setMinimumWidth(280)
        self.lst.setMinimumHeight(160)
        bl.addWidget(self.lst)
        row = QHBoxLayout()
        self.btn_all = QPushButton("All")
        self.btn_none = QPushButton("None")
        self.btn_lines = QPushButton("Lines")
        self.btn_polys = QPushButton("Polygons")
        for b in (self.btn_all, self.btn_none, self.btn_lines, self.btn_polys):
            row.addWidget(b)
        bl.addLayout(row)
        _widget_action(self.menu_refs, box)
        self.menu_refs.aboutToShow.connect(self.refresh_layers)

        # ---- tolerance
        self.menu.addSeparator()
        tol_box = QWidget()
        tl = QHBoxLayout(tol_box)
        tl.setContentsMargins(24, 2, 6, 2)
        lbl_tol = QLabel("Tolerance")
        lbl_tol.setToolTip("Max. movement")
        tl.addWidget(lbl_tol)
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setDecimals(3)
        self.sp_tol.setRange(0.001, 100000.0)
        self.sp_tol.setMinimumWidth(100)
        self.sp_tol.setToolTip("Max. movement: the largest distance any point of the edge may "
                               "move, in the units of the edited layers (shown behind the value; "
                               "no unit is shown if they use different units). The stretch itself "
                               "is bounded by the points where the edge cuts the reference.")
        tl.addWidget(self.sp_tol)
        _widget_action(self.menu, tol_box)

        # ---- options
        self.menu_opt = self.menu.addMenu("Options")
        self.menu_opt.setToolTipsVisible(True)
        for attr, _key, label, tip in OPTIONS:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setToolTip(tip)
            setattr(self, attr, act)
            self.menu_opt.addAction(act)
        self.menu_opt.addSeparator()
        eps_box = QWidget()
        el = QHBoxLayout(eps_box)
        el.setContentsMargins(24, 2, 6, 2)
        el.addWidget(QLabel("Shared-edge tolerance"))
        self.sp_eps = QDoubleSpinBox()
        self.sp_eps.setDecimals(6)
        self.sp_eps.setRange(0.000001, 10.0)
        self.sp_eps.setToolTip("Features whose boundary lies within this distance of the moved "
                               "stretch count as sharing it and are moved too.")
        el.addWidget(self.sp_eps)
        _widget_action(self.menu_opt, eps_box)

        # ---- help
        self.menu.addSeparator()
        icon = QgsApplication.getThemeIcon("/mActionHelpContents.svg")
        self.act_help = QAction(icon, "Help…", self)
        self.act_help.setToolTip("How Edge Aligner works (current mode)")
        self.act_help.triggered.connect(self._show_help)
        self.menu.addAction(self.act_help)

        self._load_settings()
        self.refresh_layers()
        self.update_units()

        self.act_edge.toggled.connect(self._mode_changed)
        self.sp_tol.valueChanged.connect(self._save_settings)
        self.sp_eps.valueChanged.connect(self._save_settings)
        for attr, _key, _label, _tip in OPTIONS:
            getattr(self, attr).toggled.connect(self._save_settings)
        self.lst.itemChanged.connect(self._save_refs)
        self.btn_all.clicked.connect(lambda: self._check_where(lambda l: True))
        self.btn_none.clicked.connect(lambda: self._check_where(lambda l: False))
        self.btn_lines.clicked.connect(lambda: self._check_where(
            lambda l: l.geometryType() == LINE))
        self.btn_polys.clicked.connect(lambda: self._check_where(
            lambda l: l.geometryType() != LINE))

        prj = QgsProject.instance()
        self._signals = [
            (prj.layersAdded, self.refresh_layers),
            (prj.layersRemoved, self.refresh_layers),
            (prj.readProject, self.refresh_layers),
            (prj.cleared, self.refresh_layers),
            (self.iface.mapCanvas().destinationCrsChanged, self.update_units),
            (self.iface.currentLayerChanged, self.update_units),
        ]
        for sig, slot in self._signals:
            sig.connect(slot)

    # ------------------------------------------------------------ public
    def mode(self):
        return self.MODE_SELECTED if self.act_sel.isChecked() else self.MODE_EDGE

    def current_settings(self):
        prj = QgsProject.instance()
        refs = []
        for i in range(self.lst.count()):
            item = self.lst.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                layer = prj.mapLayer(item.data(Qt.ItemDataRole.UserRole))
                if layer is not None and is_supported_layer(layer):
                    refs.append(layer)
        return SimpleNamespace(mode=self.mode(), tol=self.sp_tol.value(),
                               eps=self.sp_eps.value(), neighbours=self.act_nbr.isChecked(),
                               prefer_other=self.act_other.isChecked(),
                               through_junctions=self.act_junc.isChecked(), ref_layers=refs)

    def set_status(self, text):
        """Hover previews and results go to the QGIS status bar."""
        self.last_status = text
        try:
            self.iface.statusBarIface().showMessage(text, 8000)
        except (AttributeError, RuntimeError):    # no status bar (e.g. QGIS is closing)
            pass

    def cleanup(self):
        for sig, slot in self._signals:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    # ------------------------------------------------------------ layers
    def refresh_layers(self, *args):
        stored, _ok = QgsProject.instance().readEntry(PROJECT_SCOPE, PROJECT_KEY, "")
        checked = set(filter(None, (stored or "").split(";")))
        self.lst.blockSignals(True)
        self.lst.clear()
        for layer in sorted(project_layers(), key=lambda l: l.name().lower()):
            kind = "line" if layer.geometryType() == LINE else "polygon"
            item = QListWidgetItem("{}  ({})".format(layer.name(), kind))
            item.setData(Qt.ItemDataRole.UserRole, layer.id())
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(Qt.CheckState.Checked if layer.id() in checked
                               else Qt.CheckState.Unchecked)
            self.lst.addItem(item)
        self.lst.blockSignals(False)
        self._update_refs_label()
        self.update_units()

    def _check_where(self, predicate):
        prj = QgsProject.instance()
        self.lst.blockSignals(True)
        for i in range(self.lst.count()):
            item = self.lst.item(i)
            layer = prj.mapLayer(item.data(Qt.ItemDataRole.UserRole))
            on = layer is not None and predicate(layer)
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.lst.blockSignals(False)
        self._save_refs()

    def _save_refs(self, *args):
        ids = [self.lst.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self.lst.count())
               if self.lst.item(i).checkState() == Qt.CheckState.Checked]
        QgsProject.instance().writeEntry(PROJECT_SCOPE, PROJECT_KEY, ";".join(ids))
        self._update_refs_label()

    def _update_refs_label(self):
        n = sum(1 for i in range(self.lst.count())
                if self.lst.item(i).checkState() == Qt.CheckState.Checked)
        self.menu_refs.setTitle("References ({})".format(n) if n else "References (none)")

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        s = QgsSettings()
        mode = int(_to_float(s.value(SETTINGS + "mode", 0), 0))
        (self.act_sel if mode == self.MODE_SELECTED else self.act_edge).setChecked(True)
        self.sp_tol.setValue(_to_float(s.value(SETTINGS + "tolerance", 2.0), 2.0))
        self.sp_eps.setValue(_to_float(s.value(SETTINGS + "shared_tolerance", 0.001), 0.001))
        for attr, key, _label, _tip in OPTIONS:
            getattr(self, attr).setChecked(_to_bool(s.value(SETTINGS + key, True), True))
        self._update_mode_label()

    def _save_settings(self, *args):
        s = QgsSettings()
        s.setValue(SETTINGS + "mode", self.mode())
        s.setValue(SETTINGS + "tolerance", self.sp_tol.value())
        s.setValue(SETTINGS + "shared_tolerance", self.sp_eps.value())
        for attr, key, _label, _tip in OPTIONS:
            s.setValue(SETTINGS + key, getattr(self, attr).isChecked())

    def _mode_changed(self, *args):
        self._update_mode_label()
        self._save_settings()

    def _update_mode_label(self):
        edge = self.mode() == self.MODE_EDGE
        self.menu_mode.setTitle("Mode: " + ("Clicked edge" if edge else "Selected features"))
        self.menu_mode.setToolTip(HINT_EDGE if edge else HINT_SELECTED)

    def update_units(self, *args):
        """Unit shown behind the tolerance. The tolerance is applied in the CRS of the edited
        layers (not of the map), so their unit is shown: the unit of the layers in edit mode;
        if none is edited, of the active layer; else of the map. No unit is shown if the
        edited layers use different units."""
        layers = editable_layers()
        if not layers:
            active = self.iface.activeLayer()
            layers = [active] if is_supported_layer(active) else []
        try:
            if layers:
                units = {layer.crs().mapUnits() for layer in layers}
            else:
                units = {self.iface.mapCanvas().mapSettings().destinationCrs().mapUnits()}
            abbr = QgsUnitTypes.toAbbreviatedString(units.pop()) if len(units) == 1 else ""
        except (AttributeError, RuntimeError, TypeError):
            abbr = ""
        suffix = " " + abbr if abbr else ""
        if suffix != self.sp_tol.suffix():
            self.sp_tol.setSuffix(suffix)

    def _show_help(self):
        hint = HINT_EDGE if self.mode() == self.MODE_EDGE else HINT_SELECTED
        QMessageBox.information(self, "Edge Aligner", hint + "\n\n" + HINT_GENERAL)
