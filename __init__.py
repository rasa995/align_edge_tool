# -*- coding: utf-8 -*-


def classFactory(iface):
    from .align_edge_plugin import AlignEdgePlugin
    return AlignEdgePlugin(iface)
