# -*- coding: utf-8 -*-
"""Plugin entry: Align Edge toolbar, menu entry, map tool."""

import os

from qgis.PyQt.QtGui import QIcon

try:  # Qt6
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from .align_tool import AlignEdgeMapTool
from .align_toolbar import AlignEdgeToolbar

MENU = "&Align Edge"


class AlignEdgePlugin:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.toolbar = None
        self.tool = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "Align Edge", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip("Align Edge: move an edge onto a reference boundary")
        self.action.triggered.connect(self._toggle)

        self.toolbar = AlignEdgeToolbar(self.iface, self.action, self.iface.mainWindow())
        self.iface.addToolBar(self.toolbar)
        self.iface.addPluginToVectorMenu(MENU, self.action)

        self.tool = AlignEdgeMapTool(self.iface, self.toolbar)
        self.tool.setAction(self.action)

    def _toggle(self, checked):
        canvas = self.iface.mapCanvas()
        if checked:
            self.toolbar.refresh_layers()
            canvas.setMapTool(self.tool)
        elif canvas.mapTool() is self.tool:
            canvas.unsetMapTool(self.tool)

    def unload(self):
        canvas = self.iface.mapCanvas()
        if self.tool is not None:
            if canvas.mapTool() is self.tool:
                canvas.unsetMapTool(self.tool)
            self.tool.cleanup()
        if self.action is not None:
            self.iface.removePluginVectorMenu(MENU, self.action)
        if self.toolbar is not None:
            self.toolbar.cleanup()
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
        self.tool = None
        self.toolbar = None
        self.action = None
