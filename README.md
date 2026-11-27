# Align Edge (QGIS 3.44 – 4.x)

Interactive editing tool that moves an edge onto the boundary of a reference
feature. Built on QGIS core only (no third-party libraries).

## Install
Plugins > Manage and Install Plugins > Install from ZIP > `align_edge_v<version>.zip`.
The **Align Edge toolbar** appears (like the Snapping toolbar; dock it among the
toolbars or drag it out to float it; View > Toolbars shows or hides it):

`[Align Edge] [Mode ▾] [References ▾] Tolerance [ 2.000 m ] [Options ▾] [?]`

- **Align Edge** – switches the map tool on and off.
- **Mode ▾** – *Clicked edge* or *Selected features → clicked reference*.
- **References ▾** – checkable list of all line and polygon layers, with the
  buttons All / None / Lines / Polygons. The button shows how many are ticked.
- **Tolerance** – max. movement, in the unit of the layers being edited (shown behind
  the value; no unit is shown if the edited layers use different units).
- **Options ▾** – *Prefer a reference from another layer*, *Follow references
  through junctions*, *Move coincident features of other layers*, and the
  *Shared-edge tolerance*.
- **?** – short instructions for the current mode.

Results and errors appear in the message bar; the hover preview is described in
the status bar at the bottom of the QGIS window.

## Use
1. Toggle editing on the layer(s) you want to change (lines and/or polygons).
2. Under **References ▾**, tick the **reference layers**. They are never modified.
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
     the whole stretch that will move (solid magenta) and the part of the
     reference it will be moved onto (dashed orange); no preview means the click
     would not align anything there.
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
- All involved layers must use the same CRS.
- Features whose boundary coincides with the reference at the click position
  stay fixed as well (e.g. both polygons along a shared reference boundary).
- On failure, the reference that was used flashes orange and is named in the
  message bar.

## Current limitations
- Geometries with Z/M values or curves are not supported (reported, not changed).
- Without "Follow references through junctions", one reference feature per
  click (one polygon ring, or a chain of line features joined end to end).
- Vertices are not added to the reference feature where the aligned edge starts
  or ends on one of its segments.

## License
Author: Rasa.H. No third-party code included.
