# -*- coding: utf-8 -*-
"""
Geometry core of the Edge Aligner plugin.

Pure Python, no QGIS imports, so the logic can be tested without QGIS and
does not depend on any QGIS API version.

Conventions
-----------
* Coordinates are (x, y) tuples in the CRS of the layers being edited.
* A polygon ring is stored WITHOUT its closing vertex (Ring.closed = True).
  A line part is stored as it is (Ring.closed = False).
* "Reference" = the fixed boundary we align to (one polygon ring or one
  merged line). It is never modified.
* "Run" = a contiguous stretch of vertices of the edited ring whose vertices
  all lie within the alignment tolerance of the reference.
"""

import heapq
import math
from bisect import bisect_left, bisect_right

TINY = 1e-12           # identical coordinates
MEASURE_EPS = 1e-9     # identical positions along a line


# --------------------------------------------------------------------------
# Basic helpers
# --------------------------------------------------------------------------

def dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def seg_dist(p, a, b):
    """Distance from p to segment a-b.
    Returns (distance, t in [0,1] along a->b, closest point)."""
    ax, ay = a
    dx = b[0] - ax
    dy = b[1] - ay
    l2 = dx * dx + dy * dy
    if l2 <= 0.0:
        t = 0.0
    else:
        t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
    qx = ax + t * dx
    qy = ay + t * dy
    return math.hypot(p[0] - qx, p[1] - qy), t, (qx, qy)


def dedupe(pts, closed=False):
    """Remove consecutive duplicate points (and a closing duplicate if closed)."""
    out = []
    for p in pts:
        if out and dist(out[-1], p) <= TINY:
            continue
        out.append(p)
    if closed:
        while len(out) > 1 and dist(out[0], out[-1]) <= TINY:
            out.pop()
    return out


def polyline_cum(pts):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + dist(pts[i - 1], pts[i]))
    return cum


def nearest_on_polyline(pts, p):
    """Brute force nearest point on an open polyline.
    Returns (distance, measure, point, segment index)."""
    if len(pts) == 1:
        return dist(p, pts[0]), 0.0, pts[0], 0
    best = None
    m0 = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg_len = dist(a, b)
        d, t, q = seg_dist(p, a, b)
        if best is None or d < best[0]:
            best = (d, m0 + t * seg_len, q, i)
        m0 += seg_len
    return best


def carry_zm(orig, new_pts):
    """Z/M values for the vertices of a changed line or ring.

    orig: [(x, y, z, m), ...] of the original line or ring (a closed ring
    repeats its first vertex at the end); z or m is NaN where unknown.
    new_pts: [(x, y), ...] of the new geometry.
    Returns [(z, m), ...]: a vertex the original already had (same XY) keeps
    its own values; any other vertex gets the values interpolated at its
    nearest position on the original. Values are never taken from the
    reference, and unknown values stay unknown (NaN)."""
    exact = {}
    for x, y, z, m in orig:
        exact.setdefault((x, y), (z, m))
    xy = [(v[0], v[1]) for v in orig]
    out = []
    for p in new_pts:
        p = (p[0], p[1])
        if p in exact:
            out.append(exact[p])
            continue
        if len(orig) == 1:
            out.append((orig[0][2], orig[0][3]))
            continue
        best = None
        for i in range(len(xy) - 1):
            d, t, _q = seg_dist(p, xy[i], xy[i + 1])
            if best is None or d < best[0]:
                best = (d, i, t)
        _d, i, t = best
        a, b = orig[i], orig[i + 1]
        out.append((a[2] + t * (b[2] - a[2]), a[3] + t * (b[3] - a[3])))
    return out


def polygon_area(pts):
    """Unsigned area of a simple ring (shoelace); pts with or without the
    closing vertex."""
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_ring(pts, p):
    """True if p lies inside the closed ring pts (ray casting; pts without
    the closing duplicate). Points on the boundary may go either way."""
    x, y = p
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < xi + (y - yi) * (xj - xi) / (yj - yi):
            inside = not inside
        j = i
    return inside


def bbox_of(pts, pad=0.0):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def bbox_union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def bbox_intersects(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def in_bbox(p, bb):
    return bb[0] <= p[0] <= bb[2] and bb[1] <= p[1] <= bb[3]


def _with_midpoints(pts):
    out = []
    for i, p in enumerate(pts):
        out.append(p)
        if i + 1 < len(pts):
            q = pts[i + 1]
            out.append(((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0))
    return out


def directed_hausdorff(a_pts, b_pts):
    worst = 0.0
    for p in _with_midpoints(a_pts):
        d = nearest_on_polyline(b_pts, p)[0]
        if d > worst:
            worst = d
    return worst


def hausdorff(a_pts, b_pts):
    """Discrete Hausdorff distance (vertices + segment midpoints)."""
    return max(directed_hausdorff(a_pts, b_pts), directed_hausdorff(b_pts, a_pts))


# --------------------------------------------------------------------------
# Reference linework
# --------------------------------------------------------------------------

class Linework:
    """One reference ring or line with a simple grid index for fast
    nearest-point queries."""

    MAX_CELLS_PER_SEGMENT = 400
    MAX_QUERY_CELLS = 10000

    def __init__(self, pts, closed, cell):
        pts = dedupe(list(pts))
        if closed and len(pts) > 2 and dist(pts[0], pts[-1]) > TINY:
            pts.append(pts[0])
        self.pts = pts
        self.closed = bool(closed) and len(pts) > 3
        self.cum = polyline_cum(pts)
        self.length = self.cum[-1] if pts else 0.0
        self.cell = max(float(cell), 1e-9)
        self._grid = {}
        self._long = []
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            ix0, iy0 = self._cell_of(min(a[0], b[0]), min(a[1], b[1]))
            ix1, iy1 = self._cell_of(max(a[0], b[0]), max(a[1], b[1]))
            if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > self.MAX_CELLS_PER_SEGMENT:
                self._long.append(i)
                continue
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    self._grid.setdefault((ix, iy), []).append(i)

    def bbox(self, pad=0.0):
        return bbox_of(self.pts, pad)

    def _cell_of(self, x, y):
        return int(math.floor(x / self.cell)), int(math.floor(y / self.cell))

    def _candidates(self, p, r):
        ix0, iy0 = self._cell_of(p[0] - r, p[1] - r)
        ix1, iy1 = self._cell_of(p[0] + r, p[1] + r)
        if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > self.MAX_QUERY_CELLS:
            return range(len(self.pts) - 1)
        found = set(self._long)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                found.update(self._grid.get((ix, iy), ()))
        return found

    def nearest(self, p, max_dist):
        """Nearest point within max_dist: (distance, measure, point) or None."""
        best = None
        for i in self._candidates(p, max_dist):
            d, t, q = seg_dist(p, self.pts[i], self.pts[i + 1])
            if d <= max_dist and (best is None or d < best[0]):
                best = (d, self.cum[i] + t * (self.cum[i + 1] - self.cum[i]), q)
        return best

    def point_at(self, m):
        pts, cum = self.pts, self.cum
        if m <= 0.0:
            return pts[0]
        if m >= self.length:
            return pts[-1]
        i = bisect_right(cum, m) - 1
        i = min(max(i, 0), len(pts) - 2)
        seg = cum[i + 1] - cum[i]
        if seg <= 0.0:
            return pts[i]
        t = (m - cum[i]) / seg
        a, b = pts[i], pts[i + 1]
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))

    def substring(self, m_from, m_to):
        """Points from measure m_from to m_to (reversed if m_from > m_to)."""
        if m_from <= m_to:
            i0 = bisect_right(self.cum, m_from + MEASURE_EPS)
            i1 = bisect_left(self.cum, m_to - MEASURE_EPS)
            out = [self.point_at(m_from)] + self.pts[i0:i1] + [self.point_at(m_to)]
            return dedupe(out)
        return list(reversed(self.substring(m_to, m_from)))

    def candidate_paths(self, m_from, m_to):
        """Possible ways along the reference between two measures.
        Open line: one way. Closed ring: both directions around the ring."""
        if abs(m_from - m_to) <= MEASURE_EPS:
            return []
        if not self.closed:
            return [self.substring(m_from, m_to)]
        total = self.length
        if m_to > m_from:
            fwd = self.substring(m_from, m_to)
            bwd = self.substring(m_from, 0.0) + self.substring(total, m_to)[1:]
        else:
            fwd = self.substring(m_from, total) + self.substring(0.0, m_to)[1:]
            bwd = self.substring(m_from, m_to)
        return [dedupe(fwd), dedupe(bwd)]


def contains_segment(lw, a, b, eps):
    """True if segment a-b lies on the linework (ends and midpoint)."""
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    return all(lw.nearest(p, eps) is not None for p in (a, b, mid))


def dedupe_near(pts, eps, keep=(), closed=False):
    """Remove consecutive vertices closer than eps to each other where at
    least one of the two is in `keep` (the points of a new edge); the point
    in `keep` wins. Other vertices are never touched."""
    keep = set(keep)
    out = []
    for p in pts:
        if out and dist(out[-1], p) <= eps and (p in keep or out[-1] in keep):
            if p in keep and out[-1] not in keep:
                out[-1] = p
            continue
        out.append(p)
    if closed:
        while (len(out) > 3 and dist(out[0], out[-1]) <= eps
               and (out[0] in keep or out[-1] in keep)):
            if out[-1] in keep and out[0] not in keep:
                out[0] = out[-1]
            out.pop()
    return out


def fold_backs(pts, closed, eps):
    """Vertices where the boundary turns back onto itself: one adjacent
    segment runs back along the other (a spike of zero width). Returns the
    list of such vertices. Valid-geometry checks do not always catch this."""
    n = len(pts)
    if closed and n > 1 and dist(pts[0], pts[-1]) <= TINY:
        pts = pts[:-1]
        n -= 1
    out = []
    rng = range(n) if closed else range(1, n - 1)
    for i in rng:
        u, v, w = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        if dist(u, v) <= eps or dist(v, w) <= eps:
            continue
        du, tu, _q = seg_dist(w, v, u)
        dw, tw, _q = seg_dist(u, v, w)
        if (du <= eps and tu > 0.0) or (dw <= eps and tw > 0.0):
            out.append(v)
    return out


def clip_polyline(pts, bb):
    """Pieces of the polyline made of the segments that touch the box."""
    out, cur = [], []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        sb = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        if bbox_intersects(sb, bb):
            if not cur:
                cur = [a]
            cur.append(b)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def polyline_has_segment(pts, a, b, eps):
    """True if segment a-b lies on the polyline (ends and midpoint)."""
    mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    bb = bbox_of([a, b], eps)
    near = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)
            if bbox_intersects(bbox_of([pts[i], pts[i + 1]]), bb)]
    return all(any(seg_dist(p, c, d)[0] <= eps for c, d in near) for p in (a, b, mid))


class Network(Linework):
    """Several reference boundaries combined into one noded network (used
    to follow the reference across the boundaries of several features).
    Same interface as Linework for nearest(), crossings() and build_path();
    a measure is 2 * segment index + position (0..1) on that segment."""

    def __init__(self, polylines, cell, eps):
        self.eps = max(float(eps), 1e-9)
        segs = []
        for line in polylines:
            line = dedupe(list(line))
            for i in range(len(line) - 1):
                if dist(line[i], line[i + 1]) > self.eps:
                    segs.append((line[i], line[i + 1]))
        if segs:
            # noding grid: cells of about two average segment lengths
            avg = sum(dist(a, b) for a, b in segs) / len(segs)
            segs = self._node(segs, max(2.0 * avg, 10.0 * self.eps))
        # node ids (points within eps share a node), duplicate edges dropped
        self._nodes = []
        self._node_grid = {}
        edges = {}
        for a, b in segs:
            na, nb = self._node_id(a), self._node_id(b)
            if na == nb:
                continue
            k = (min(na, nb), max(na, nb))
            if k not in edges:
                edges[k] = (na, nb)
        self.edges = list(edges.values())
        flat = []
        for na, nb in self.edges:
            flat.extend((self._nodes[na], self._nodes[nb]))
        self.pts = flat
        self.closed = False
        self.seg_len = [dist(self._nodes[a], self._nodes[b]) for a, b in self.edges]
        self.length = sum(self.seg_len)
        self.cum = [0.0] * len(flat)
        self.cell = max(float(cell), 1e-9)
        self._grid = {}
        self._long = []
        for s in range(len(self.edges)):
            i = 2 * s
            a, b = flat[i], flat[i + 1]
            ix0, iy0 = self._cell_of(min(a[0], b[0]), min(a[1], b[1]))
            ix1, iy1 = self._cell_of(max(a[0], b[0]), max(a[1], b[1]))
            if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > self.MAX_CELLS_PER_SEGMENT:
                self._long.append(i)
                continue
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    self._grid.setdefault((ix, iy), []).append(i)
        self._adj = {}
        for s, (na, nb) in enumerate(self.edges):
            self._adj.setdefault(na, []).append((nb, s))
            self._adj.setdefault(nb, []).append((na, s))

    def _node(self, segs, cell):
        """Split segments where they cross or touch each other."""
        eps = self.eps
        grid = {}
        for k, (a, b) in enumerate(segs):
            ix0, iy0 = int(math.floor((min(a[0], b[0]) - eps) / cell)), \
                int(math.floor((min(a[1], b[1]) - eps) / cell))
            ix1, iy1 = int(math.floor((max(a[0], b[0]) + eps) / cell)), \
                int(math.floor((max(a[1], b[1]) + eps) / cell))
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    grid.setdefault((ix, iy), []).append(k)
        cuts = [set() for _ in segs]
        for bucket in grid.values():
            for x in range(len(bucket)):
                i = bucket[x]
                a, b = segs[i]
                for y in range(x + 1, len(bucket)):
                    j = bucket[y]
                    c, d = segs[j]
                    for t in seg_meet(a, b, c, d, eps):
                        cuts[i].add(t)
                    for t in seg_meet(c, d, a, b, eps):
                        cuts[j].add(t)
        out = []
        for (a, b), ts in zip(segs, cuts):
            ts = sorted(t for t in ts if 0.0 < t < 1.0)
            prev = a
            for t in ts + [1.0]:
                q = b if t >= 1.0 else (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                if dist(prev, q) > eps:
                    out.append((prev, q))
                    prev = q
        return out

    def _node_id(self, p):
        eps = self.eps
        ix, iy = int(math.floor(p[0] / eps)), int(math.floor(p[1] / eps))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for nid in self._node_grid.get((ix + dx, iy + dy), ()):
                    if dist(self._nodes[nid], p) <= eps:
                        return nid
        nid = len(self._nodes)
        self._nodes.append(p)
        self._node_grid.setdefault((ix, iy), []).append(nid)
        return nid

    def _candidates(self, p, r):
        ix0, iy0 = self._cell_of(p[0] - r, p[1] - r)
        ix1, iy1 = self._cell_of(p[0] + r, p[1] + r)
        if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > self.MAX_QUERY_CELLS:
            return range(0, len(self.pts), 2)
        found = set(self._long)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                found.update(self._grid.get((ix, iy), ()))
        return found

    def nearest(self, p, max_dist):
        best = None
        for i in self._candidates(p, max_dist):
            d, t, q = seg_dist(p, self.pts[i], self.pts[i + 1])
            if d <= max_dist and (best is None or d < best[0]):
                best = (d, i + t, q)
        return best

    def point_at(self, m):
        s = min(max(int(m // 2), 0), len(self.edges) - 1)
        t = min(max(m - 2 * s, 0.0), 1.0)
        a, b = self.pts[2 * s], self.pts[2 * s + 1]
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))

    def lines_near(self, p, r):
        """Segments within r of p (for flashing the reference)."""
        out = []
        for i in self._candidates(p, r):
            if seg_dist(p, self.pts[i], self.pts[i + 1])[0] <= r:
                out.append([self.pts[i], self.pts[i + 1]])
        return out

    def substring(self, m_from, m_to):
        return [self.point_at(m_from), self.point_at(m_to)]

    def _dijkstra(self, s1, t1, s2, t2, banned):
        """Shortest route from a point on edge s1 to a point on edge s2.
        Returns the list of node ids (without the two end points) or None."""
        a1, b1 = self.edges[s1]
        a2, b2 = self.edges[s2]
        l1, l2 = self.seg_len[s1], self.seg_len[s2]
        target = {a2: t2 * l2, b2: (1.0 - t2) * l2}
        best = {}
        heap = []
        for nd, d in ((a1, t1 * l1), (b1, (1.0 - t1) * l1)):
            if d < best.get(nd, (float("inf"),))[0]:
                best[nd] = (d, None)
                heapq.heappush(heap, (d, nd))
        done = set()
        end = None
        while heap:
            d, nd = heapq.heappop(heap)
            if nd in done:
                continue
            done.add(nd)
            if nd in target:
                total = d + target[nd]
                if end is None or total < end[0]:
                    end = (total, nd)
            if end is not None and d >= end[0]:
                break
            for nb, s in self._adj.get(nd, ()):
                if s in banned or s == s1 or s == s2:
                    continue
                nd2 = d + self.seg_len[s]
                if nd2 < best.get(nb, (float("inf"),))[0]:
                    best[nb] = (nd2, (nd, s))
                    heapq.heappush(heap, (nd2, nb))
        if end is None:
            return None
        route = []
        nd = end[1]
        while nd is not None:
            route.append(nd)
            prev = best[nd][1]
            nd = prev[0] if prev else None
        route.reverse()
        return route

    def _route_edges(self, route):
        out = set()
        for x, y in zip(route, route[1:]):
            for nb, s in self._adj.get(x, ()):
                if nb == y:
                    out.add(s)
        return out

    def candidate_paths(self, m_from, m_to, k=4):
        """The shortest routes through the network between two measures
        (the shortest one plus alternatives avoiding one of its edges)."""
        if abs(m_from - m_to) <= MEASURE_EPS or not self.edges:
            return []
        s1 = min(int(m_from // 2), len(self.edges) - 1)
        s2 = min(int(m_to // 2), len(self.edges) - 1)
        p1, p2 = self.point_at(m_from), self.point_at(m_to)
        if s1 == s2:
            return [[p1, p2]] if dist(p1, p2) > TINY else []
        t1, t2 = m_from - 2 * s1, m_to - 2 * s2
        first = self._dijkstra(s1, t1, s2, t2, set())
        if first is None:
            return []
        routes = [first]
        for s in self._route_edges(first):
            if len(routes) >= k:
                break
            alt = self._dijkstra(s1, t1, s2, t2, {s})
            if alt is not None and alt not in routes:
                routes.append(alt)
        return [dedupe([p1] + [self._nodes[n] for n in r] + [p2]) for r in routes]


# --------------------------------------------------------------------------
# Editable rings
# --------------------------------------------------------------------------

class Ring:
    __slots__ = ("pts", "closed")

    def __init__(self, pts, closed):
        pts = list(pts)
        if closed and len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        self.pts = pts
        self.closed = closed

    def copy(self):
        r = Ring.__new__(Ring)
        r.pts = list(self.pts)
        r.closed = self.closed
        return r

    @property
    def n(self):
        return len(self.pts)

    @property
    def nseg(self):
        return self.n if self.closed else max(self.n - 1, 0)

    def seg(self, i):
        return self.pts[i], self.pts[(i + 1) % self.n]

    def to_list(self):
        if self.closed and self.pts:
            return self.pts + [self.pts[0]]
        return list(self.pts)

    def run_points(self, start, length):
        return [self.pts[(start + t) % self.n] for t in range(length)]

    def bbox(self, pad=0.0):
        return bbox_of(self.pts, pad)


def nearest_segment(ring, p, max_dist=None):
    """(distance, segment index, closest point) or None."""
    best = None
    for i in range(ring.nseg):
        a, b = ring.seg(i)
        if max_dist is not None:
            if (p[0] < min(a[0], b[0]) - max_dist or p[0] > max(a[0], b[0]) + max_dist or
                    p[1] < min(a[1], b[1]) - max_dist or p[1] > max(a[1], b[1]) + max_dist):
                continue
        d, _t, q = seg_dist(p, a, b)
        if max_dist is not None and d > max_dist:
            continue
        if best is None or d < best[0]:
            best = (d, i, q)
    return best


def zone(ring, run):
    """Vertex and segment indices of a run plus one vertex on each side."""
    start, length = run
    n = ring.n
    if ring.closed:
        if length >= n - 1:
            return list(range(n)), list(range(n))
        vidx = [(start - 1 + t) % n for t in range(length + 2)]
        sidx = [(start - 1 + t) % n for t in range(length + 1)]
        return vidx, sidx
    lo = max(0, start - 1)
    hi = min(n - 1, start + length)
    return list(range(lo, hi + 1)), list(range(lo, hi))


def densify(ring, points, eps, seg_indices=None):
    """Insert the given points as vertices where they lie on a segment of the
    ring (within eps, not at an existing vertex). Modifies ring in place and
    returns the number of inserted vertices."""
    if not points or ring.n < 2:
        return 0
    pb = bbox_of(points, eps)
    if seg_indices is None:
        segs = range(ring.nseg)
    else:
        segs = sorted(i for i in seg_indices if 0 <= i < ring.nseg)
    inserts = {}
    for i in segs:
        a, b = ring.seg(i)
        sb = (min(a[0], b[0]) - eps, min(a[1], b[1]) - eps,
              max(a[0], b[0]) + eps, max(a[1], b[1]) + eps)
        if not bbox_intersects(sb, pb):
            continue
        for p in points:
            if not in_bbox(p, sb):
                continue
            if dist(p, a) <= eps or dist(p, b) <= eps:
                continue
            d, t, _q = seg_dist(p, a, b)
            if d <= eps and 0.0 < t < 1.0:
                inserts.setdefault(i, {})[p] = t
    if not inserts:
        return 0
    out = []
    count = 0
    for i in range(ring.n):
        out.append(ring.pts[i])
        if i in inserts:
            for p, _t in sorted(inserts[i].items(), key=lambda kv: kv[1]):
                if dist(out[-1], p) > eps:
                    out.append(p)
                    count += 1
    ring.pts = out
    return count


def coincident_groups(ring, old_pts, eps):
    """Groups of consecutive vertices of `ring` lying on the polyline old_pts.
    Returns list of (start, length, first point, last point)."""
    if len(old_pts) < 2 or ring.n == 0:
        return []
    ob = bbox_of(old_pts, eps)

    def on_old(p):
        return in_bbox(p, ob) and nearest_on_polyline(old_pts, p)[0] <= eps

    n = ring.n
    flags = [on_old(p) for p in ring.pts]
    if not any(flags):
        return []
    link = [False] * n
    for i in range(ring.nseg):
        j = (i + 1) % n
        if flags[i] and flags[j]:
            a, b = ring.pts[i], ring.pts[j]
            link[i] = on_old(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
    if ring.closed:
        r = next((i for i in range(n) if not link[(i - 1) % n]), None)
        if r is None:
            return [(0, n, ring.pts[0], ring.pts[n - 1])]
        order = [(r + t) % n for t in range(n)]
    else:
        order = list(range(n))
    groups = []
    cur = None
    prev = None
    for i in order:
        if flags[i]:
            if cur is not None and prev is not None and link[prev]:
                cur[1] += 1
            else:
                if cur is not None:
                    groups.append(cur)
                cur = [i, 1]
        elif cur is not None:
            groups.append(cur)
            cur = None
        prev = i
    if cur is not None:
        groups.append(cur)
    return [(s, l, ring.pts[s], ring.pts[(s + l - 1) % n]) for s, l in groups]


def augment(path, measures):
    """Insert points at the given measures into the path.
    Returns (points, measures of points)."""
    cum = polyline_cum(path)
    total = cum[-1]
    ms = sorted(m for m in measures if MEASURE_EPS < m < total - MEASURE_EPS)
    out_p = [path[0]]
    out_m = [0.0]
    j = 0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        ca, cb = cum[i], cum[i + 1]
        while j < len(ms) and ms[j] < cb - MEASURE_EPS:
            m = ms[j]
            j += 1
            if m <= out_m[-1] + MEASURE_EPS:
                continue
            t = (m - ca) / (cb - ca) if cb > ca else 0.0
            out_p.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            out_m.append(m)
        while j < len(ms) and ms[j] <= cb + MEASURE_EPS:
            j += 1
        out_p.append(b)
        out_m.append(cb)
    return out_p, out_m


def index_of(aug_m, m):
    i = bisect_left(aug_m, m)
    if i <= 0:
        return 0
    if i >= len(aug_m):
        return len(aug_m) - 1
    return i if abs(aug_m[i] - m) < abs(aug_m[i - 1] - m) else i - 1


def splice(ring, reps):
    """Replace vertex ranges. reps = [(start, length, new_points)].
    Overlapping replacements are skipped (first wins). Returns a new Ring."""
    n = ring.n
    covered = [False] * n
    starts = {}
    for start, length, new_pts in reps:
        if ring.closed:
            idx = [(start + t) % n for t in range(length)]
        else:
            idx = list(range(start, min(start + length, n)))
        if not idx or any(covered[i] for i in idx):
            continue
        for i in idx:
            covered[i] = True
        starts[idx[0]] = (len(idx), list(new_pts))
    if not starts:
        return ring.copy()
    out = []
    if not ring.closed:
        i = 0
        while i < n:
            if i in starts:
                length, new = starts[i]
                out.extend(new)
                i += length
            else:
                out.append(ring.pts[i])
                i += 1
        return Ring(dedupe(out), False)
    rot = next((i for i in range(n) if not covered[i]), None)
    if rot is None:
        rot = min(starts)
    i = rot
    visited = 0
    while visited < n:
        if i in starts:
            length, new = starts[i]
            out.extend(new)
            i = (i + length) % n
            visited += length
        elif covered[i]:
            i = (i + 1) % n
            visited += 1
        else:
            out.append(ring.pts[i])
            i = (i + 1) % n
            visited += 1
    return Ring(dedupe(out, closed=True), True)


# --------------------------------------------------------------------------
# Cut points between the edited ring and the reference
# --------------------------------------------------------------------------

def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def seg_meet(a, b, c, d, eps):
    """Parameters t (0..1 along a->b) where segment a-b meets segment c-d:
    proper crossing, touching within eps, or ends of a collinear overlap."""
    ts = []
    d1x, d1y = b[0] - a[0], b[1] - a[1]
    d2x, d2y = d[0] - c[0], d[1] - c[1]
    den = _cross(d1x, d1y, d2x, d2y)
    if abs(den) > TINY:
        acx, acy = c[0] - a[0], c[1] - a[1]
        t = _cross(acx, acy, d2x, d2y) / den
        u = _cross(acx, acy, d1x, d1y) / den
        if -1e-12 <= t <= 1 + 1e-12 and -1e-12 <= u <= 1 + 1e-12:
            ts.append(min(max(t, 0.0), 1.0))
    for p in (c, d):
        dd, t, _q = seg_dist(p, a, b)
        if dd <= eps:
            ts.append(t)
    for p, t in ((a, 0.0), (b, 1.0)):
        if seg_dist(p, c, d)[0] <= eps:
            ts.append(t)
    return ts


def crossings(ring, lw, eps):
    """Sorted measures along the ring where it cuts or touches the reference."""
    pts = ring.to_list()
    cum = polyline_cum(pts)
    total = cum[-1]
    ms = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg_len = cum[i + 1] - cum[i]
        if seg_len <= 0.0:
            continue
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        for j in lw._candidates(mid, seg_len / 2.0 + eps):
            for t in seg_meet(a, b, lw.pts[j], lw.pts[j + 1], eps):
                ms.append(cum[i] + t * seg_len)
    ms.sort()
    out = []
    for m in ms:
        if ring.closed and m >= total - eps:
            m = 0.0
        if not out or m - out[-1] > eps:
            out.append(m)
    if ring.closed and len(out) > 1 and out[0] <= eps and total - out[-1] <= eps:
        out.pop()
    return sorted(set(out))


def _events(ring, lw, tol, eps):
    """Ordered events along the ring: ('x', m) cut point, ('v', m, near)
    vertex not on the reference. Returns (events, total length)."""
    pts = ring.to_list()
    cum = polyline_cum(pts)
    total = cum[-1]
    xs = crossings(ring, lw, eps)
    ev = [(m, "x", True) for m in xs]
    for j in range(ring.n):
        m = cum[j]
        if any(abs(m - x) <= eps or (ring.closed and abs(total - m + x) <= eps) for x in xs):
            continue
        near = lw.nearest(ring.pts[j], tol) is not None
        ev.append((m, "v", near))
    ev.sort(key=lambda e: e[0])
    return ev, total


def click_stretch(ring, lw, tol, eps, click_point):
    """Stretch of the ring around the click, bounded by the nearest cut points
    with the reference (or, without a cut point on a side, by the last vertex
    within tolerance). Returns ((m_from, m_to), None) or (None, message)."""
    pts = ring.to_list()
    cum = polyline_cum(pts)
    hit = nearest_segment(ring, click_point)
    if hit is None:
        return None, "Empty geometry."
    k = hit[1]
    mc = cum[k] + dist(pts[k], hit[2])
    ev, total = _events(ring, lw, tol, eps)
    if ring.closed:
        fwd = [e for e in ev if e[0] > mc + eps] + [(e[0] + total,) + e[1:] for e in ev
                                                    if e[0] <= mc + eps]
        bwd = [e for e in reversed(ev) if e[0] < mc - eps] + \
              [(e[0] - total,) + e[1:] for e in reversed(ev) if e[0] >= mc - eps]
    else:
        fwd = [e for e in ev if e[0] > mc + eps]
        bwd = [e for e in reversed(ev) if e[0] < mc - eps]

    def walk(events):
        last_near = None
        for m, kind, near in events:
            if kind == "x":
                return m, True
            if near:
                last_near = m
            else:
                return last_near, True
        return last_near, False       # ran to the line end / around the ring

    m_to, stop_f = walk(fwd)
    m_from, stop_b = walk(bwd)
    if ring.closed and not (stop_f and stop_b):
        return None, ("The whole ring lies within the tolerance of the reference without "
                      "cutting it; it was not changed.")
    if m_to is None or m_from is None or m_to - m_from <= eps:
        return None, ("Near the click, the edge is farther than the alignment tolerance "
                      "from the reference boundary (no cut point within reach); it was not "
                      "changed.")
    if ring.closed and m_to - m_from >= total - eps:
        return None, "The stretch would cover the whole ring; it was not changed."
    return (m_from, m_to), None


def all_stretches(ring, lw, tol, eps):
    """All stretches of the ring that can be aligned (selected-features mode):
    pieces between consecutive stops (cut points and vertices beyond the
    tolerance), trimmed to vertices within tolerance next to far vertices."""
    ev, total = _events(ring, lw, tol, eps)
    stops = [i for i, e in enumerate(ev) if e[1] == "x" or not e[2]]
    out = []
    if ring.closed:
        if not stops:
            return [], ["A whole ring lies within the tolerance of the reference without "
                        "cutting it; it was not changed."]
        seqs = []
        for a_i, s in enumerate(stops):
            e_i = stops[(a_i + 1) % len(stops)]
            idx = list(range(s, e_i + 1)) if e_i > s else \
                list(range(s, len(ev))) + list(range(0, e_i + 1))
            seqs.append(idx)
    else:
        bounds = [-1] + stops + [len(ev)]
        seqs = [list(range(max(bounds[i], 0), min(bounds[i + 1], len(ev) - 1) + 1))
                for i in range(len(bounds) - 1)]
    for idx in seqs:
        if not idx:
            continue
        ms = []
        offset = 0.0
        prev = None
        for i in idx:
            m = ev[i][0]
            if prev is not None and m < prev - eps:
                offset = total
            prev = m
            ms.append((m + offset, ev[i][1], ev[i][2]))
        # left end
        first = ms[0]
        if first[1] == "x" or first[2]:
            m_from = first[0]
            body = ms[1:]
        else:
            body = ms[1:]
            m_from = None
        # right end
        last = body[-1] if body else None
        if last is not None and (last[1] == "x" or last[2]):
            m_to = last[0]
            inner = body[:-1]
        else:
            inner = body[:-1] if body else []
            m_to = None
        near_inner = [e[0] for e in inner if e[1] == "v" and e[2]]
        if m_from is None:
            m_from = near_inner[0] if near_inner else None
        if m_to is None:
            m_to = near_inner[-1] if near_inner else None
        if m_from is None or m_to is None or m_to - m_from <= eps:
            continue
        out.append((m_from, m_to))
    return out, []


def anchor_at(ring, m, eps):
    """Make sure the ring has a vertex at measure m (inserting one on the
    segment if needed). Modifies the ring; returns the vertex coordinates."""
    pts = ring.to_list()
    cum = polyline_cum(pts)
    total = cum[-1]
    if ring.closed:
        m = m % total if total > 0 else 0.0
    for j in range(ring.n):
        if abs(cum[j] - m) <= eps:
            return ring.pts[j]
    if ring.closed and abs(total - m) <= eps:
        return ring.pts[0]
    i = min(max(bisect_right(cum, m) - 1, 0), len(pts) - 2)
    a, b = pts[i], pts[i + 1]
    seg = cum[i + 1] - cum[i]
    t = (m - cum[i]) / seg if seg > 0 else 0.0
    p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    ring.pts.insert(i + 1, p)
    return p


def locate_run(ring, anchor_from, anchor_to):
    """Vertex run (start, length) from anchor_from forward to anchor_to."""
    try:
        i0 = ring.pts.index(anchor_from)
        i1 = ring.pts.index(anchor_to)
    except ValueError:
        return None
    n = ring.n
    if ring.closed:
        length = (i1 - i0) % n + 1
    else:
        if i1 <= i0:
            return None
        length = i1 - i0 + 1
    return (i0, length) if length >= 2 else None


# --------------------------------------------------------------------------
# Path between the ends of a run
# --------------------------------------------------------------------------

STATUS_TEXT = {
    "far": "An end of the stretch is not within the tolerance of the reference.",
    "degenerate": "No usable path along the reference: both ends of the stretch project "
                  "onto the same point of the reference, or the matching part of the "
                  "reference is much longer than the stretch.",
    "aligned": "The stretch is already aligned with the reference.",
    "mismatch": "Part of the stretch would move {h:.3f} map units, more than the alignment "
                "tolerance; it was not changed.",
}


def build_path(lw, run_pts, tol, eps):
    """Part of the reference between the projections of the run's ends,
    following the reference in the run's direction.
    Returns (points, "ok", h) or (None, status, h). h = largest movement."""
    s = lw.nearest(run_pts[0], tol + eps)
    e = lw.nearest(run_pts[-1], tol + eps)
    if s is None or e is None:
        return None, "far", None
    run_len = polyline_cum(run_pts)[-1]
    best = None
    for cand in lw.candidate_paths(s[1], e[1]):
        if len(cand) < 2:
            continue
        if polyline_cum(cand)[-1] > 3.0 * run_len + 10.0 * tol:
            continue
        h = hausdorff(run_pts, cand)
        if best is None or h < best[0]:
            best = (h, cand)
    if best is None:
        return None, "degenerate", None
    if best[0] <= eps:
        return None, "aligned", best[0]
    if best[0] > tol + eps:
        return None, "mismatch", best[0]
    return best[1], "ok", best[0]


# --------------------------------------------------------------------------
# Main operation
# --------------------------------------------------------------------------

class NeighbourItem:
    """A ring of another feature that may share the edge being moved.
    payload is opaque data of the caller (layer, feature id, ...)."""
    __slots__ = ("ring", "readonly", "payload")

    def __init__(self, ring, readonly, payload):
        self.ring = ring
        self.readonly = readonly
        self.payload = payload


class AlignResult:
    def __init__(self):
        self.ring = None            # new primary ring, None = nothing changed
        self.neighbours = []        # [(NeighbourItem, new Ring)]
        self.readonly_hits = []     # [NeighbourItem] sharing the edge but not editable
        self.paths = []             # new edges (for highlighting)
        self.messages = []


def align_ring(ring, lw, tol, eps, click_point=None, neighbour_provider=None,
               max_iter=4, hole=False):
    """Align one ring (or line part) onto the reference linework.

    click_point given -> the stretch around the click, between the nearest
                         cut points with the reference.
    click_point None  -> every alignable stretch (selected-features mode).
    neighbour_provider(bbox) -> list of new NeighbourItem in that bbox;
    None disables moving coincident features.
    hole: the ring is an interior ring of a polygon (only matters for nodes,
    see fixed_node below).
    The tolerance is the largest distance any point may move.
    """
    res = AlignResult()
    work = ring.copy()
    items = []

    if click_point is not None:
        st, msg = click_stretch(work, lw, tol, eps, click_point)
        if st is None:
            res.messages.append(msg)
            return res
        stretches = [st]
    else:
        stretches, msgs = all_stretches(work, lw, tol, eps)
        res.messages.extend(msgs)
        if not stretches:
            return res

    anchors = []
    for m_from, m_to in stretches:
        anchors.append((anchor_at(work, m_from, eps), anchor_at(work, m_to, eps)))

    def find_runs():
        out = []
        for a_from, a_to in anchors:
            r = locate_run(work, a_from, a_to)
            if r is not None:
                out.append(r)
        return out

    runs = find_runs()
    if not runs:
        return res

    if neighbour_provider is not None:
        # Make the edited ring and its coincident neighbours share the same
        # vertices around the stretch, so they stay identical after the move.
        for _ in range(max_iter):
            zone_v, zone_s = set(), set()
            for r in runs:
                v, s_ = zone(work, r)
                zone_v.update(v)
                zone_s.update(s_)
            zone_pts = [work.pts[i] for i in sorted(zone_v)]
            items.extend(neighbour_provider(bbox_of(zone_pts, tol + eps)))
            zb = bbox_of(zone_pts, eps)
            inserted = 0
            nbr_pts = []
            for it in items:
                if it.readonly:
                    continue
                inserted += densify(it.ring, zone_pts, eps)
                nbr_pts.extend(p for p in it.ring.pts if in_bbox(p, zb))
            inserted += densify(work, nbr_pts, eps, zone_s)
            if inserted == 0:
                break
            runs = find_runs()
            if not runs:
                return res

    built = []
    for r in runs:
        run_pts = work.run_points(*r)
        path, status, h = build_path(lw, run_pts, tol, eps)
        if path is None:
            if status == "aligned" and click_point is None:
                continue
            res.messages.append(STATUS_TEXT[status].format(h=h or 0.0))
            continue
        built.append([r, run_pts, path])
    if not built:
        return res

    nbr_reps = {}
    readonly_hit = set()
    for bi, b in enumerate(built):
        _r, run_pts, path = b
        measures = []
        for ii, it in enumerate(items):
            groups = coincident_groups(it.ring, run_pts, eps)
            if not groups:
                continue
            if it.readonly:
                readonly_hit.add(ii)
                continue
            for start, length, pa, pb in groups:
                ma = nearest_on_polyline(path, pa)[1]
                mb = ma if length == 1 else nearest_on_polyline(path, pb)[1]
                measures.extend((ma, mb))
                nbr_reps.setdefault(ii, []).append((start, length, bi, ma, mb, pa, pb))
        aug_pts, aug_m = augment(path, measures)
        b.extend([aug_pts, aug_m])
        res.paths.append(aug_pts)

    keep = set(p for b in built for p in b[3])
    res.ring = splice(work, [(b[0][0], b[0][1], b[3]) for b in built])
    res.ring = Ring(dedupe_near(res.ring.pts, eps, keep, res.ring.closed), res.ring.closed)
    new_pts = res.ring.to_list()

    def fixed_node(p, run_pts):
        """A node inside the stretch (where a neighbour's shared part ends)
        that the edited polygon leaves behind (it shrinks there) keeps its
        position: the neighbours connect it straight to the new edge, so
        their common boundary beyond the node is not changed."""
        if not ring.closed:
            return False
        if dist(p, run_pts[0]) <= eps or dist(p, run_pts[-1]) <= eps:
            return False
        if nearest_on_polyline(new_pts, p)[0] <= eps:
            return False
        return point_in_ring(res.ring.pts, p) == hole

    for ii, reps in nbr_reps.items():
        it = items[ii]
        new_reps = []
        for start, length, bi, ma, mb, pa, pb in reps:
            run_pts, aug_pts, aug_m = built[bi][1], built[bi][3], built[bi][4]
            ia, ib = index_of(aug_m, ma), index_of(aug_m, mb)
            fix_a = fixed_node(pa, run_pts)
            if length == 1:
                new = [pa] if fix_a else [aug_pts[ia]]
            else:
                if ia == ib:
                    new = [aug_pts[ia]]
                elif ia < ib:
                    new = aug_pts[ia:ib + 1]
                else:
                    new = aug_pts[ib:ia + 1][::-1]
                if fix_a:
                    new = [pa] + new
                if fixed_node(pb, run_pts):
                    new = new + [pb]
            new_reps.append((start, length, new))
        nr = splice(it.ring, new_reps)
        res.neighbours.append((it, Ring(dedupe_near(nr.pts, eps, keep, nr.closed), nr.closed)))
    res.readonly_hits = [items[i] for i in sorted(readonly_hit)]
    return res
