# Edge Aligner (QGIS 3.44 – 4.x)

Interactive editing tool that moves an edge onto the boundary of a reference
feature. Built on QGIS core only (no third-party libraries).

## Install
Plugins > Manage and Install Plugins > Install from ZIP > `edge_aligner_v<version>.zip`.
The **Edge Aligner** button appears in its own toolbar (like the Vertex Tool
button: one icon with a drop-down arrow; View > Toolbars shows or hides it).
Click the icon to switch the map tool on and off; click the arrow **▾** to open
the settings:

- **Mode ▸** – *Clicked edge* or *Selected features → clicked reference*.
- **References ▸** – checkable list of all line and polygon layers, with the
  buttons All / None / Lines / Polygons. The entry shows how many are ticked.
- **Tolerance** – max. movement, in the unit of the layers being edited (shown behind
  the value; no unit is shown if the edited layers use different units).
- **Options ▸** – *Prefer a reference from another layer*, *Follow references
  through junctions*, *Move coincident features of other layers*, and the
  *Shared-edge tolerance*.
- **Align all within tolerance…** – batch alignment to a base layer (see below).
- **Help…** – short instructions for the current mode.

Results and errors appear in the message bar; while hovering, an info box next
to the cursor describes what the click will do.

## Use
1. Toggle editing on the layer(s) you want to change (lines and/or polygons).
2. Under **▾ > References**, tick the **reference layers**. They are never modified.
   A layer may be both edited and reference (within-layer alignment).
   To align polygons to lines (and lines to polygons), tick **both** the line and
   the polygon layer. With "Prefer a reference from another layer" on (default),
   a clicked polygon edge goes to the nearest line, a clicked line goes to the
   nearest polygon boundary. The clicked feature is always the one that moves.
3. Set the **max. movement (tolerance)** in layer map units (e.g. metres): the
   largest distance any point of the edge may move. It must be at least as large
   as the widest gap between the edge and the reference along the stretch.
4. Activate the tool and choose a mode:
   - **Clicked edge** – click the edge to move. While you hover, a preview shows
     the whole stretch that will move (solid magenta), the part of the
     reference it will be moved onto (dashed orange) and the area in between
     (shaded). An info box next to the cursor names the features that will move
     (including coincident ones that move along), the reference, the largest
     movement and the area between the edges – or, in orange, why the click
     would not align anything or would be refused. The click is computed in full
     for the box (nothing is written), so it says exactly what the click will do.
     Where several reference boundaries are within the tolerance, the box shows
     "Reference 1 of N": press **Tab** / **Shift+Tab** to pick another one; the
     click then uses that one. Moving to another edge starts again with the
     default choice.
     Where two polygons share the clicked edge, the polygon on the side you
     click (the one containing the click point) is the one that moves; the
     same applies to the reference. Click slightly inside the polygon you mean.
     Where a line lies exactly on a polygon edge, the polygon edge is the one that
     is moved; the line follows only along its coincident part. A line is moved
     on its own only when you click it where no polygon edge lies under it. Note
     that a clicked line without a cut point on one side is moved up to its last
     vertex within the tolerance, so with a large tolerance that can be the
     whole line.
     The part of the edge between the nearest points (on both sides of the click)
     where it cuts or touches the reference is replaced by that part of the
     reference. Where the edge does not cut the reference on a side (e.g. a line
     end), the part ends at the last vertex within the tolerance.
     With "Follow references through junctions" (default on), a reference
     line that meets other lines at a junction (e.g. a fault that forks) can be
     continued onto the connecting lines; the continuation that allows the
     longest stretch within the tolerance is used. In addition, the boundaries
     of all ticked reference features near the stretch are combined: where the
     edge has to follow one feature to a corner and then another one (e.g. a
     line crossing a small area bounded by two polygons), it follows both. The
     combined boundaries are used when the single nearest reference cannot align
     the stretch, or when another reference boundary meets the edge closer to the
     click; the stretch then ends there (like at a node). References are never
     changed by this.
   - **Selected features → clicked reference** – select the features, then click
     the reference boundary. All stretches of the selected features within the
     tolerance of that boundary are aligned.
5. Undo with Ctrl+Z (one edit command per changed layer; QGIS undo is per layer).

## Align all within tolerance
For many small gaps at once, open **▾ > Align all within tolerance…**:
1. Choose the **base layer** – the layer treated as correct. It is never changed.
2. The **layers to align** are all other line/polygon layers in edit mode.
3. Choose the **area** (current map view or whole layers) and the **tolerance**.
4. **Scan** lists every feature with a stretch within the tolerance of a base
   layer boundary, with the number of stretches and the largest movement.
   Nothing is changed yet. Double-click a row to zoom to the feature.
5. **Align ticked** aligns each ticked row exactly as *Selected features* mode
   would (that feature selected, that base boundary clicked): read again from
   the layer at that moment, with the same safety rules, coincident features
   moving along, and one undo step per row. Rows that are refused keep the
   reason in the *Result* column; the others carry on.
## Coincident features
With "Move coincident features of other layers" on, every feature whose
boundary shares the moved stretch (within the shared-edge tolerance, default
0.001) moves with it, including lines that only end on the stretch. Vertices are
made identical on both sides, so shared boundaries stay coincident.
Where the stretch passes a node (a corner where a neighbour's shared part ends,
e.g. where two neighbours meet) and the edited polygon shrinks there, the node
keeps its position: the neighbours are connected straight from the node to the
new edge, so their common boundary beyond the node is not changed.
Only layers that are **in edit mode** move along. Coincident features of layers
that are not in edit mode stay unchanged and are listed in the message bar, so
put every layer that should follow in edit mode first.

## Safety rules
- If any changed geometry would become invalid (e.g. a sliver collapsing), the
  whole operation is cancelled and nothing is written.
- The same applies if a changed boundary would fold back onto itself (a spike
  of zero width, which GEOS still reports as valid), and – with "Move coincident
  features" on – if a changed polygon would get a new overlap with another
  polygon of its layer, or leave a new enclosed gap between polygons of its
  layer. Growing or closing an existing gap is allowed. The message names the
  feature and the position.
- Vertices closer than the shared-edge tolerance to a vertex of the new edge are
  merged, so no near-duplicate vertices are written.
- Stretches already aligned, or whose reference deviates more than 5 × tolerance,
  are left unchanged.
- All or nothing across layers: if writing fails in one layer (or raises an
  error), the layers already written are taken back.
- A feature that was changed or deleted between reading and writing is not
  overwritten; nothing is written and you are asked to try again.
- Features whose boundary coincides with the reference at the click position
  stay fixed as well (e.g. both polygons along a shared reference boundary).
- On failure, the reference that was used flashes orange and is named in the
  message bar.

## Z/M, curve layers and CRSs
- **Z/M values** are kept. Vertices the feature already had keep their own Z/M;
  new vertices get values interpolated along the feature's original edge (never
  taken from the reference). Unknown values stay unknown.
- **Curve layers** (CompoundCurve, CurvePolygon, MultiCurve, MultiSurface) are
  supported as long as the feature has only straight segments; it is written
  back as the layer's curve type. Features with real arcs are not changed.
- **Reference layers may use another CRS** than the edited layers: they are
  reprojected on the fly (they are never written, so no stored vertex shifts).
  Layers that move along as coincident features, and the layers of the selected
  features in *Selected features* mode, must use the same CRS as the edited
  feature.

## Current limitations
- Features with arcs (circular strings) are not supported (reported, not changed).
- Without "Follow references through junctions", one reference feature per
  click (one polygon ring, or a chain of line features joined end to end).
- Vertices are not added to the reference feature where the aligned edge starts
  or ends on one of its segments.

## Tests
From the plugin folder, with the Python of QGIS (the core tests also run with
any Python 3; the QGIS tests are then skipped):

    "C:\Program Files\QGIS 3.44.x\bin\python-qgis-ltr.bat" -m unittest discover -s tests
    "C:\Program Files\QGIS 4.x\bin\python-qgis.bat" -m unittest discover -s tests

The `tests` folder is not included in the release ZIP.

## License
Author: Rasa.H. No third-party code included.
