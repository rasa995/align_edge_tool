# -*- coding: utf-8 -*-
"""'Align all within tolerance' dialog: pick a base layer that stays fixed,
scan the layers in edit mode for every stretch within the tolerance of a
base layer boundary, review the list, then align the ticked ones.

Each row is aligned exactly like a click in 'Selected features' mode (the
feature selected, the base boundary clicked): read again from the layer at
that moment, same safety checks, one undoable edit per changed layer."""

from types import SimpleNamespace

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .align_tool import _unit, editable_layers, project_layers

SETTINGS = "EdgeAligner/batch_"
COLUMNS = ("Layer", "Feature", "Aligned to", "Stretches", "Largest movement", "Result")


class BatchAlignDialog(QDialog):

    def __init__(self, iface, tool, controls, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.tool = tool
        self.controls = controls
        self.items = []
        self.setWindowTitle("Edge Aligner – Align all within tolerance")
        self.resize(760, 480)

        lay = QVBoxLayout(self)
        intro = QLabel("Aligns every stretch of the layers in edit mode that lies within the "
                       "tolerance of a boundary of the base layer. The base layer is never "
                       "changed. Scan first, check the list (double-click a row to zoom to "
                       "it), then align the ticked rows. Each row is one undoable edit.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        self.cb_base = QComboBox()
        self.cb_base.setToolTip("The layer treated as correct. Its boundaries are the "
                                "references; it is never changed.")
        form.addRow("Base layer (stays fixed)", self.cb_base)
        self.lbl_layers = QLabel()
        self.lbl_layers.setWordWrap(True)
        form.addRow("Layers to align", self.lbl_layers)
        self.cb_area = QComboBox()
        self.cb_area.addItems(["Current map view", "Whole layers"])
        form.addRow("Area", self.cb_area)
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setDecimals(3)
        self.sp_tol.setRange(0.001, 100000.0)
        self.sp_tol.setToolTip("Max. movement, in the units of the layers being aligned.")
        form.addRow("Tolerance", self.sp_tol)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.btn_scan = QPushButton("Scan")
        self.btn_all = QPushButton("Tick all")
        self.btn_none = QPushButton("Tick none")
        self.btn_align = QPushButton("Align ticked")
        self.btn_align.setEnabled(False)
        for b in (self.btn_scan, self.btn_all, self.btn_none):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_align)
        lay.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        lay.addWidget(self.table, 1)

        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        lay.addWidget(self.lbl_summary)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.btn_scan.clicked.connect(self.scan)
        self.btn_align.clicked.connect(self.align)
        self.btn_all.clicked.connect(lambda: self._tick(True))
        self.btn_none.clicked.connect(lambda: self._tick(False))
        self.table.cellDoubleClicked.connect(self._zoom_to)
        self.cb_base.currentIndexChanged.connect(self._update_layers)
        self.refresh()

    # ------------------------------------------------------------ setup
    def refresh(self):
        """Fill the layer choices and the tolerance (call before showing)."""
        s = QgsSettings()
        cfg = self.controls.current_settings()
        last = s.value(SETTINGS + "base", "")
        refs = [l.id() for l in cfg.ref_layers]
        self.cb_base.blockSignals(True)
        self.cb_base.clear()
        for layer in sorted(project_layers(), key=lambda l: l.name().lower()):
            self.cb_base.addItem(layer.name(), layer.id())
        idx = self.cb_base.findData(last)
        if idx < 0 and refs:
            idx = self.cb_base.findData(refs[0])
        self.cb_base.setCurrentIndex(max(idx, 0))
        self.cb_base.blockSignals(False)
        self.cb_area.setCurrentIndex(int(s.value(SETTINGS + "area", 0) or 0))
        self.sp_tol.setValue(cfg.tol)
        self.sp_tol.setSuffix(self.controls.sp_tol.suffix())
        self._update_layers()

    def _base_layer(self):
        return QgsProject.instance().mapLayer(self.cb_base.currentData() or "")

    def _target_layers(self):
        base = self._base_layer()
        return [l for l in editable_layers() if base is None or l.id() != base.id()]

    def _update_layers(self, *args):
        names = [l.name() for l in self._target_layers()]
        self.lbl_layers.setText(", ".join(names) if names else
                                "<i>No other line or polygon layer is in edit mode.</i>")

    def _cfg(self):
        cfg = self.controls.current_settings()
        values = dict(vars(cfg))
        values["tol"] = self.sp_tol.value()
        return SimpleNamespace(**values)

    # ------------------------------------------------------------ actions
    def scan(self):
        base = self._base_layer()
        layers = self._target_layers()
        self._update_layers()
        if base is None or not layers:
            self.lbl_summary.setText("Choose a base layer and put the layers to align in "
                                     "edit mode.")
            return
        cfg = self._cfg()
        if cfg.tol <= cfg.eps:
            self.lbl_summary.setText("The tolerance must be larger than the shared-edge "
                                     "tolerance (Options).")
            return
        s = QgsSettings()
        s.setValue(SETTINGS + "base", base.id())
        s.setValue(SETTINGS + "area", self.cb_area.currentIndex())
        extent = self.iface.mapCanvas().extent() if self.cb_area.currentIndex() == 0 else None

        dlg = QProgressDialog("Scanning features…", "Cancel", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(400)
        cancelled = []

        def progress(done, total):
            dlg.setMaximum(max(total, 1))
            dlg.setValue(done)
            QApplication.processEvents()
            if dlg.wasCanceled():
                cancelled.append(True)
                return False
            return True

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.items = self.tool.batch_scan(base, layers, cfg, extent, progress)
        finally:
            QApplication.restoreOverrideCursor()
            dlg.close()
        self._fill()
        text = "{} feature(s) can be aligned to '{}'.".format(len(self.items), base.name())
        if cancelled:
            text = "Scan cancelled; the list is incomplete. " + text
        self.lbl_summary.setText(text)

    def _fill(self):
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.items))
        for r, it in enumerate(self.items):
            unit = _unit(it.layer.crs())
            first = QTableWidgetItem(it.layer.name())
            first.setFlags(first.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            first.setCheckState(Qt.CheckState.Checked)
            cells = [first, QTableWidgetItem(str(it.fid)), QTableWidgetItem(it.ref.label),
                     QTableWidgetItem(str(it.count)),
                     QTableWidgetItem("{:.3f}{}".format(it.h, unit)), QTableWidgetItem("")]
            for c, cell in enumerate(cells):
                if c:
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                self.table.setItem(r, c, cell)
        self.btn_align.setEnabled(bool(self.items))

    def _tick(self, on):
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(state)

    def align(self):
        cfg = self._cfg()
        rows = [r for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.CheckState.Checked]
        done = failed = 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for r in rows:
                it = self.items[r]
                try:
                    ok, msg = self.tool.batch_apply(it, cfg)
                except Exception as ex:     # pylint: disable=broad-exception-caught
                    ok, msg = False, "{}: {}".format(type(ex).__name__, ex)
                self.table.item(r, 5).setText(("Aligned – " if ok else "Skipped – ") + msg)
                self.table.item(r, 5).setToolTip(msg)
                self.table.item(r, 0).setCheckState(Qt.CheckState.Unchecked)
                if ok:
                    done += 1
                else:
                    failed += 1
                QApplication.processEvents()
        finally:
            QApplication.restoreOverrideCursor()
        self.iface.mapCanvas().refresh()
        self.lbl_summary.setText(
            "{} aligned, {} skipped. Undo with Ctrl+Z per layer (one step per row). Skipped "
            "rows keep their reason in the Result column; scan again after fixing them."
            .format(done, failed))

    def _zoom_to(self, row, _col):
        if 0 <= row < len(self.items):
            it = self.items[row]
            canvas = self.iface.mapCanvas()
            canvas.zoomToFeatureIds(it.layer, [it.fid])
            canvas.flashFeatureIds(it.layer, [it.fid])
