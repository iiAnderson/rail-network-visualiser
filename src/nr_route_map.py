"""Avanti-style route map for any station, drawn on the National Rail route map's own lines.

    poetry run python3 nr_route_map.py RDG DON BRI --patterns national.csv --tiplocs tiplocs_geo.csv

Routes come from station_route_map.discover_routes. Each route is traced over the schematic
network extracted from the National Rail PDF (data/nr_schematic.json, see
extract_nr_schematic.py), so loops, junctions and parallel corridors are real rather than
inferred. The diagram starts at a generous scale and shrinks step by step while labels and
lanes stay clear of each other.
"""
import argparse
import bisect
import collections
import difflib
import functools
import heapq
import json
import math
import os
import re

import numpy as np
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

import station_route_map as base
from station_route_map import (PALETTE, PITCH, LINE_W, TRANSITION, FONT, K_STOPS, WINDOW, Data,
                               discover_routes, badge, esc, f, fmt_tph, label_candidate, legend,
                               route_title)

HERE = os.path.dirname(os.path.abspath(__file__))
NR_JSON = os.path.join(HERE, "..", "data", "nr_schematic.json")
HOPS = 11
SCALES = [110, 98, 88, 80, 72, 65, 59, 54, 50, 46, 43, 40, 37, 34]


# ------------------------------------------------------------------ the PDF network
def norm(s):
    s = (s.lower().replace("&", "and").replace("ﬁ", "fi").replace("ﬀ", "ff")
         .replace("ﬂ", "fl").replace("'", "").replace("’", ""))
    s = re.sub(r"\b(rail station|station)\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def strip_paren(s):
    return norm(re.sub(r"\(.*?\)", "", s))


class Network:
    def __init__(self, path=NR_JSON):
        g = json.load(open(path))
        self.node = {n["id"]: n for n in g["nodes"]}
        self.adj = collections.defaultdict(dict)  # a -> b -> length
        self.edge = {}                            # (a, b) -> pts oriented a->b
        for e in g["edges"]:
            a, b, pts = e["a"], e["b"], e["pts"]
            L = sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pts, pts[1:]))
            self.adj[a][b] = self.adj[b][a] = L
            self.edge[(a, b)] = pts
            self.edge[(b, a)] = pts[::-1]
        self.index = collections.defaultdict(dict)  # normalised name -> {node id: node}
        for n in g["nodes"]:
            if n["name"]:
                nm = re.sub(r"\s+[a-z]$", "", n["name"])  # trailing icon letter
                for k in {norm(nm), strip_paren(nm)}:
                    self.index[k][n["id"]] = n
        self.anchors = None
        un = [n for n in g["nodes"] if n["station"] and not n["name"]]
        self.un_ids = [n["id"] for n in un]
        self.un_xy = np.array([[n["x"], n["y"]] for n in un]) if un else np.zeros((0, 2))
        self.claimed = set()

    def build_anchors(self, data):
        pts = []
        for crs, nm in data.names.items():
            if crs not in data.xy:
                continue
            c = self.exact(nm)
            if len(c) == 1:
                n = c[0]
                pts.append((data.xy[crs][0], -data.xy[crs][1], n["x"], n["y"]))
        self.anchors = np.array(pts)

    def exact(self, name):
        keys = [norm(name), strip_paren(name), norm(re.sub(r"^london ", "", name, flags=re.I))]
        for k in keys:
            c = self.index.get(k)
            if c:
                return list(c.values())
        return []

    def predict(self, e, n):
        """Local affine from OSGB to PDF coordinates using the nearest anchors."""
        a = self.anchors
        d = np.hypot(a[:, 0] - e, a[:, 1] - n)
        idx = np.argsort(d)[:14]
        A = np.c_[a[idx, 0], a[idx, 1], np.ones(len(idx))]
        M, *_ = np.linalg.lstsq(A, a[idx, 2:4], rcond=None)
        return tuple(np.array([e, n, 1.0]) @ M)

    def match(self, crs, name, data):
        pred = None
        if crs in data.xy and self.anchors is not None:
            pred = self.predict(data.xy[crs][0], -data.xy[crs][1])
        cands = self.exact(name)
        if not cands:
            # interchange labels in the PDF run into their neighbours ("East Croydon East Croydon"):
            # accept a label that contains the whole name, if it is where the station should be
            k = " %s " % norm(re.sub(r"^london ", "", name, flags=re.I))
            near = [n for n in self.node.values() if n["name"] and k in " %s " % norm(n["name"])]
            if pred is not None:
                near = [n for n in near if math.hypot(n["x"] - pred[0], n["y"] - pred[1]) < 60]
            if pred is not None or len(near) == 1:
                cands = near
        if not cands:
            keys = list(self.index)
            for k in difflib.get_close_matches(norm(name), keys, n=3, cutoff=0.9):
                cands += list(self.index[k].values())
        if pred is not None and cands:
            cands.sort(key=lambda n: math.hypot(n["x"] - pred[0], n["y"] - pred[1]))
            if math.hypot(cands[0]["x"] - pred[0], cands[0]["y"] - pred[1]) > 400:
                cands = []
        if cands:
            if pred is None:
                cands.sort(key=lambda n: -len(self.adj[n["id"]]))
            return cands[0]["id"]
        # rescue: the PDF dot exists but its label did not attach; take the nearest unnamed dot
        if pred is not None and len(self.un_ids):
            d = np.hypot(self.un_xy[:, 0] - pred[0], self.un_xy[:, 1] - pred[1])
            order = np.argsort(d)
            for i in order[:3]:
                nid = self.un_ids[i]
                if d[i] <= 30 and nid not in self.claimed and len(self.adj[nid]) >= 1:
                    self.claimed.add(nid)
                    return nid
        return None

    def path(self, a, b):
        dist, prev = {a: 0.0}, {}
        pq = [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist[u]:
                continue
            for v, w in self.adj[u].items():
                nd = d + w + 0.5
                if nd < dist.get(v, 1e18):
                    dist[v], prev[v] = nd, u
                    heapq.heappush(pq, (nd, v))
        if b not in dist:
            return None
        out = [b]
        while out[-1] != a:
            out.append(prev[out[-1]])
        return out[::-1]


# ------------------------------------------------------------------ geometry helpers
def polylen(pts):
    return sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(pts, pts[1:]))


def offset_poly(pts, d):
    """Offset to the left of travel (y down: left normal is (dy, -dx)) by d."""
    out = []
    n = len(pts)
    segs = []
    for p, q in zip(pts, pts[1:]):
        L = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
        segs.append(((q[0] - p[0]) / L, (q[1] - p[1]) / L))
    for i, p in enumerate(pts):
        if i == 0:
            t = segs[0]
            nx, ny = t[1], -t[0]
            k = 1.0
        elif i == n - 1:
            t = segs[-1]
            nx, ny = t[1], -t[0]
            k = 1.0
        else:
            a, b = segs[i - 1], segs[i]
            nx, ny = (a[1] + b[1]), -(a[0] + b[0])
            m = math.hypot(nx, ny)
            if m < 1e-6:
                nx, ny, k = a[1], -a[0], 1.0
            else:
                nx, ny = nx / m, ny / m
                cosh = nx * a[1] + ny * (-a[0])
                k = 1.0 / max(cosh, 0.5)
        out.append((p[0] + nx * d * k, p[1] + ny * d * k))
    return out


def trim_from(pts, s):
    """Polyline from arclength s on (and the tangent there)."""
    acc = 0.0
    for i in range(len(pts) - 1):
        L = math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        if acc + L >= s:
            t = (s - acc) / L if L else 0
            p = (pts[i][0] + t * (pts[i + 1][0] - pts[i][0]), pts[i][1] + t * (pts[i + 1][1] - pts[i][1]))
            return [p] + pts[i + 1:]
        acc += L
    return [pts[-1], pts[-1]]


def tangent(pts, end):
    if end:
        p, q = pts[-2], pts[-1]
    else:
        p, q = pts[0], pts[1]
    L = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
    return (q[0] - p[0]) / L, (q[1] - p[1]) / L


def simplify(pts, tol=0.15):
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > tol:
            out.append(p)
    if len(out) == 1:
        out.append(pts[-1])
    return out


def pts_d(pts):
    return "L".join("%s %s" % (f(x), f(y)) for x, y in pts)


def candidates(x, y, half, w, tang):
    """Label placements for a station, best first for the local line direction."""
    from shapely.geometry import box as _box
    h = 16.0
    def hbox(tx, ty, anchor):
        return _box(tx - 2, ty - h + 3, tx + w, ty + 4) if anchor == "start" else _box(tx - w, ty - h + 3, tx + 2, ty + 4)
    c = {
        "R": ("R", x + half, y + 4.5, "start", 0),
        "L": ("L", x - half, y + 4.5, "end", 0),
        "SE": ("SE", x + half * 0.5 + 3, y + half + 13, "start", 0),
        "NE": ("NE", x + half * 0.5 + 3, y - half - 4, "start", 0),
        "SW": ("SW", x - half * 0.5 - 3, y + half + 13, "end", 0),
        "NW": ("NW", x - half * 0.5 - 3, y - half - 4, "end", 0),
        "U": ("U", x + 2, y - half, "start", -45),
        "D": ("D", x - 2, y + half + 2, "end", -45),
        "U2": ("U2", x + 2, y - half, "start", -90),
        "D2": ("D2", x - 2, y + half + 2, "end", -90),
    }
    dx, dy = tang
    ax, ay = abs(dx), abs(dy)
    if ay > 2 * ax:        # vertical-ish
        order = ["R", "L", "SE", "NW", "NE", "SW", "U", "D", "U2", "D2"]
    elif ax > 2 * ay:      # horizontal-ish
        order = ["U", "D", "NE", "SW", "NW", "SE", "R", "L", "U2", "D2"]
    elif dx * dy < 0:      # / diagonal (y down)
        order = ["SE", "NW", "R", "L", "NE", "SW", "U", "D", "U2", "D2"]
    else:                  # \ diagonal
        order = ["NE", "SW", "R", "L", "SE", "NW", "U", "D", "U2", "D2"]
    out = []
    for k in order:
        mode, tx, ty, anchor, rot = c[k]
        poly = hbox(tx, ty, anchor) if rot == 0 else base.rot_box(tx, ty, w, h, rot, anchor)
        out.append((mode, poly, tx, ty, anchor, rot))
    return out


# ------------------------------------------------------------------ scene
class Scene:
    def __init__(self, data, nr, hub_crs, routes):
        self.data, self.nr, self.hub_crs = data, nr, hub_crs
        self.routes = routes
        self.prepare()

    def prepare(self):
        data, nr = self.data, self.nr
        nr.build_anchors(data) if nr.anchors is None else None
        self.stn = {}  # crs -> node id
        for r in self.routes:
            for c in r["seq"]:
                if c not in self.stn:
                    m = nr.match(c, data.names.get(c, c), data)
                    if m is not None:
                        self.stn[c] = m
        self.hub = self.stn.get(self.hub_crs)
        if self.hub is None:
            raise RuntimeError("hub not found on the route map")
        good = []
        for r in self.routes:
            seq = [c for c in r["seq"] if c in self.stn]
            if self.hub_crs not in seq or len(seq) < 2:
                continue
            nodes = [self.stn[seq[0]]]
            ok = True
            for c in seq[1:]:
                p = nr.path(nodes[-1], self.stn[c])
                if not p:
                    ok = False
                    break
                nodes += p[1:]
            if not ok:
                continue
            nodes = despur(nodes, self.hub)
            nodes, cut = trim_hops(nodes, self.hub, nr, HOPS)
            r = dict(r)
            r["nodes"] = nodes
            r["calls_n"] = {self.stn[c] for c in r["calls"] if c in self.stn} | set(cut)
            good.append(r)
        self.routes = good
        # edges used and outward orientation
        self.used = set()
        for r in good:
            for a, b in zip(r["nodes"], r["nodes"][1:]):
                self.used.add(frozenset((a, b)))
        adj = collections.defaultdict(set)
        for e in self.used:
            a, b = tuple(e)
            adj[a].add(b)
            adj[b].add(a)
        self.dist = {self.hub: 0}
        q = collections.deque([self.hub])
        while q:
            u = q.popleft()
            for v in adj[u]:
                if v not in self.dist:
                    self.dist[v] = self.dist[u] + 1
                    q.append(v)
        self.adj = adj
        votes = collections.defaultdict(float)
        for r in good:
            nodes = r["nodes"]
            hi = nodes.index(self.hub)
            for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
                out = (b, a) if i < hi else (a, b)  # direction of travel away from the hub
                votes[out] += r["tph"]
        self.canon = {}
        for e in self.used:
            a, b = tuple(e)
            u, v = (a, b) if votes[(a, b)] >= votes[(b, a)] else (b, a)
            self.canon[(a, b)] = self.canon[(b, a)] = (u, v)
        self.order_lanes()

    def pdf_tangent(self, u, v, at_u=True):
        pts = self.nr.edge[(u, v)]
        s = 5.0
        if at_u:
            q = trim_from(pts, min(s, polylen(pts) / 2))[0]
            p = pts[0]
        else:
            r = pts[::-1]
            q = trim_from(r, min(s, polylen(pts) / 2))[0]
            p = pts[-1]
        L = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
        return (q[0] - p[0]) / L, (q[1] - p[1]) / L

    def order_lanes(self):
        """Left-to-right lane order per canonical edge from where routes diverge outward."""
        nodes_of = {r["id"]: r["nodes"] for r in self.routes}
        lanes = {}
        by_edge = collections.defaultdict(list)
        for r in self.routes:
            for a, b in zip(r["nodes"], r["nodes"][1:]):
                if r["id"] not in by_edge[self.canon[(a, b)]]:
                    by_edge[self.canon[(a, b)]].append(r["id"])
        P = self.nr.node

        def lateral(d, t):
            return t[0] * d[1] + t[1] * (-d[0])  # dot with left normal (dy, -dx)

        def step(rid, node, forward_dir_from, outward):
            """next node of route rid after `node` travelling away from forward_dir_from."""
            seq = nodes_of[rid]
            idxs = [i for i, n in enumerate(seq) if n == node]
            for i in idxs:
                if i > 0 and seq[i - 1] == forward_dir_from and i + 1 < len(seq):
                    return seq[i + 1]
                if i + 1 < len(seq) and seq[i + 1] == forward_dir_from and i > 0:
                    return seq[i - 1]
            return None

        def compare(u, v, r1, r2):
            # forward (outward) divergence
            prev, cur = u, v
            for _ in range(60):
                n1, n2 = step(r1, cur, prev, True), step(r2, cur, prev, True)
                if n1 is None or n2 is None:
                    break
                if n1 != n2:
                    din = self.pdf_tangent(prev, cur, at_u=False)
                    t1, t2 = self.pdf_tangent(cur, n1), self.pdf_tangent(cur, n2)
                    l1, l2 = lateral(din, t1), lateral(din, t2)
                    if abs(l1 - l2) > 1e-6:
                        return -1 if l1 > l2 else 1
                    return 0
                prev, cur = cur, n1
            # backward divergence (routes arrived from different sides)
            prev, cur = v, u
            for _ in range(60):
                n1, n2 = step(r1, cur, prev, False), step(r2, cur, prev, False)
                if n1 is None or n2 is None:
                    break
                if n1 != n2:
                    dout = self.pdf_tangent(cur, prev)  # direction of outward travel at cur
                    t1, t2 = self.pdf_tangent(cur, n1), self.pdf_tangent(cur, n2)
                    l1, l2 = lateral(dout, t1), lateral(dout, t2)
                    if abs(l1 - l2) > 1e-6:
                        return -1 if l1 > l2 else 1
                    return 0
                prev, cur = cur, n1
            return 0

        for (u, v), rids in by_edge.items():
            lanes[(u, v)] = sorted(rids, key=functools.cmp_to_key(lambda x, y: compare(u, v, x, y)))
        # propagate outward: an edge keeps its predecessor's order for the routes they share, so a
        # route can only change side where it genuinely merges or diverges (no crossings elsewhere)
        for (u, v) in sorted(lanes, key=lambda e: (self.dist.get(e[0], 99), self.dist.get(e[1], 99))):
            cur = lanes[(u, v)]
            preds = [e for e in lanes if e[1] == u and e != (u, v)]
            if not preds:
                continue
            preds = [e for e in preds if set(lanes[e]) & set(cur)]
            if not preds:
                continue
            tout = self.pdf_tangent(u, v)
            # incoming corridors keep their routes together, ordered by the side they approach from
            preds.sort(key=lambda e: -lateral(tout, self.pdf_tangent(u, e[0])))
            order = []
            for e in preds:
                order += [r for r in lanes[e] if r in cur and r not in order]
            for r in [r for r in cur if r not in order]:
                pos = len(order)
                for i, o in enumerate(order):
                    if compare(u, v, r, o) < 0:
                        pos = i
                        break
                order.insert(pos, r)
            lanes[(u, v)] = order
        self.lanes = lanes

    # ---- geometry at a spacing D (px between neighbouring stations)
    def build(self, D):
        """Keep the PDF's edge directions, but clamp edge lengths to [D, 1.5 D] and re-solve
        node positions by least squares, so dense and sparse parts of the map both fit."""
        nr = self.nr
        nodes = list(self.dist)
        ix = {n: i for i, n in enumerate(nodes)}
        edges = sorted(set(self.canon.values()))
        A = np.zeros((len(edges) + 1, len(nodes)))
        bx = np.zeros(len(edges) + 1)
        by = np.zeros(len(edges) + 1)
        for k, (u, v) in enumerate(edges):
            pu, pv = nr.node[u], nr.node[v]
            dx, dy = pv["x"] - pu["x"], pv["y"] - pu["y"]
            L = math.hypot(dx, dy)
            both_stn = pu["station"] and pv["station"]
            lo = D if both_stn else max(14.0, 0.28 * D)
            hi = 1.5 * D if both_stn else 1.0 * D
            tgt = min(max(L * D / 20.0, lo), hi)
            if L < 1e-6:
                dx, dy, L = 1.0, 0.0, 1.0
            A[k, ix[u]], A[k, ix[v]] = -1.0, 1.0
            bx[k], by[k] = tgt * dx / L, tgt * dy / L
        A[-1, ix[self.hub]] = 1.0  # pin the hub at the origin
        xs = np.linalg.lstsq(A, bx, rcond=None)[0]
        ys = np.linalg.lstsq(A, by, rcond=None)[0]
        self.s = D
        self.pos = {n: (float(xs[ix[n]]), float(ys[ix[n]])) for n in nodes}
        self.edge_pts = {}
        for (u, v) in edges:
            pts = nr.edge[(u, v)]
            za, zb = complex(*pts[0]), complex(*pts[-1])
            na, nb = complex(*self.pos[u]), complex(*self.pos[v])
            k = (nb - na) / (zb - za) if abs(zb - za) > 1e-9 else 1.0
            q = []
            for p in pts:
                z = na + k * (complex(*p) - za)
                q.append((z.real, z.imag))
            self.edge_pts[(u, v)] = q
        self.off = {}  # (rid, u, v) -> offset polyline oriented u->v (canonical)
        for (u, v), order in self.lanes.items():
            n = len(order)
            for i, rid in enumerate(order):
                self.off[(rid, u, v)] = simplify(offset_poly(self.edge_pts[(u, v)], ((n - 1) / 2 - i) * PITCH))

    def node_tangent(self, n):
        """Average direction of the lines through station n (sign-aligned)."""
        ref, acc = None, [0.0, 0.0]
        for m in self.adj[n]:
            u, v = self.canon[(n, m)]
            pts = self.edge_pts[(u, v)]
            t = tangent(pts, end=(v == n)) if u != n else tangent(pts, end=False)
            if v == n:
                pass
            if ref is None:
                ref = t
            if t[0] * ref[0] + t[1] * ref[1] < 0:
                t = (-t[0], -t[1])
            acc[0] += t[0]
            acc[1] += t[1]
        L = math.hypot(*acc) or 1.0
        return (acc[0] / L, acc[1] / L)

    def route_geometry(self, r):
        """(svg path pieces split at the hub, dots, end positions)."""
        nodes = r["nodes"]
        segs = []
        for a, b in zip(nodes, nodes[1:]):
            u, v = self.canon[(a, b)]
            q = self.off[(r["id"], u, v)]
            segs.append(q if (a, b) == (u, v) else q[::-1])
        pieces = []
        dots = []
        hubi = nodes.index(self.hub)
        cur = []
        for i, q in enumerate(segs):
            if i == 0:
                cur = list(q)
            else:
                if i == hubi:  # leaving the hub: end the previous piece, start the next
                    pieces.append(cur)
                    cur = list(q)
                    continue
                prev_end, nxt = cur[-1], q
                length = polylen(q)
                T = min(TRANSITION, 0.45 * length)
                if math.hypot(prev_end[0] - q[0][0], prev_end[1] - q[0][1]) < 0.3:
                    cur += q[1:]
                else:
                    t1 = tangent(cur, True)
                    tail = trim_from(q, T)
                    t2 = tangent(tail, False)
                    e = tail[0]
                    d = math.hypot(e[0] - prev_end[0], e[1] - prev_end[1])
                    k = max(d * 0.5, 4.0)
                    c1 = (prev_end[0] + t1[0] * k, prev_end[1] + t1[1] * k)
                    c2 = (e[0] - t2[0] * k, e[1] - t2[1] * k)
                    bez = [bezier(prev_end, c1, c2, e, t / 8) for t in range(1, 9)]
                    cur += bez + tail[1:]
        pieces.append(cur)
        # dots: end of the edge arriving at each called node (start of first edge for node 0)
        for i, n in enumerate(nodes):
            if n in r["calls_n"] and n != self.hub:
                q = segs[i - 1][-1] if i > 0 else segs[0][0]
                dots.append((n, q))
        return pieces, dots


def bezier(p0, p1, p2, p3, t):
    a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def trim_hops(nodes, hub, nr, hops):
    """Keep `hops` station nodes (called or passed) either side of the hub; returns the trimmed
    path and the new end nodes where it was cut."""
    hi = nodes.index(hub)
    cut = []

    def keep(seq):
        n_st, out = 0, []
        for n in seq:
            out.append(n)
            if nr.node[n]["station"]:
                n_st += 1
                if n_st > hops:
                    cut.append(n)
                    break
        return out

    right = keep(nodes[hi:])
    left = keep(nodes[:hi + 1][::-1])
    return left[::-1] + right[1:], cut


def despur(nodes, keep=None):
    out = []
    for n in nodes:
        if len(out) >= 2 and out[-2] == n and out[-1] != keep:
            out.pop()
        elif not out or out[-1] != n:
            out.append(n)
    return out


# ------------------------------------------------------------------ render at a scale
def render(scene, s, data, colours, with_svg=True):
    scene.build(s)
    scene.dbg = collections.Counter()
    scene.dbg_names = []
    P = scene.pos
    hub = scene.hub
    routes = scene.routes
    # geometry
    paths, dots, tph = [], [], collections.defaultdict(float)
    end_leaf, end_label = collections.defaultdict(list), collections.defaultdict(list)
    for r in routes:
        pieces, rdots = scene.route_geometry(r)
        for pc in pieces:
            if len(pc) >= 2:
                paths.append((r["id"], pc))
        for n_, q in rdots:
            dots.append((r["id"], q, n_))
        for c in r["calls"]:
            if c in scene.stn:
                tph[scene.stn[c]] += r["tph"]
        nodes = r["nodes"]
        for end, nxt in ((nodes[0], nodes[1]), (nodes[-1], nodes[-2])):
            if end == hub:
                continue
            leaf = len(scene.adj[end]) == 1
            (end_leaf if leaf else end_label)[end].append(r["id"])
    stn_name = {}
    for c, n in scene.stn.items():
        stn_name.setdefault(n, data.names.get(c, c))
    # junction stations (3+ edges) are drawn as interchange pills instead of per-lane dots
    junction = {n for n in scene.dist if n != hub and scene.nr.node[n]["station"] and len(scene.adj[n]) >= 3
                and tph.get(n)}
    # ...and so is any station where two route dots would land on top of each other
    by_node = collections.defaultdict(list)
    for d in dots:
        by_node[d[2]].append(d[1])
    for n, pts in by_node.items():
        if n != hub and tph.get(n) and any(math.hypot(a[0] - b[0], a[1] - b[1]) < 8.5
                                           for i, a in enumerate(pts) for b in pts[i + 1:]):
            junction.add(n)
    dots = [d for d in dots if d[2] not in junction]
    # lane widths
    obstacles, edge_geoms = [], []
    bundle = collections.defaultdict(float)
    for (u, v), order in scene.lanes.items():
        w = len(order) * PITCH / 2 + 2.5
        ls = LineString(scene.edge_pts[(u, v)])
        core = ls
        if ls.length > 70:
            core = LineString([ls.interpolate(30).coords[0]] + [c for c in ls.coords
                              if 30 < ls.project(Point(c)) < ls.length - 30] + [ls.interpolate(ls.length - 30).coords[0]])
        edge_geoms.append((u, v, core.buffer(w, cap_style=2)))
        obstacles.append(ls.buffer(w, cap_style=2))
        for n in (u, v):
            bundle[n] = max(bundle[n], len(order) * PITCH / 2)
    hub_half = bundle[hub] + 9
    pill = Point(P[hub]).buffer(hub_half * 1.5)
    obstacles.append(pill)
    pills = {}  # node -> (cx, cy, tangent angle deg, half length across the lanes)
    for n in junction:
        tx_, ty_ = scene.node_tangent(n)
        pills[n] = (P[n][0], P[n][1], math.degrees(math.atan2(ty_, tx_)), bundle[n] + 6)
        a = math.atan2(ty_, tx_)
        c, sn = math.cos(a), math.sin(a)
        hl, hw = 8.0, bundle[n] + 6
        corners = [(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)]
        from shapely.geometry import Polygon as _Poly
        obstacles.append(_Poly([(P[n][0] + x * c - y * sn, P[n][1] + x * sn + y * c) for x, y in corners]).buffer(4))
    for _, (x, y), _n in dots:
        obstacles.append(Point(x, y).buffer(5))
    leaf_pos = {}
    for end, lst in end_leaf.items():
        # outward direction = away from the neighbour
        nb = next(iter(scene.adj[end]))
        d = (P[end][0] - P[nb][0], P[end][1] - P[nb][1])
        L = math.hypot(*d) or 1
        ux, uy = d[0] / L, d[1] / L
        leaf_pos[end] = (ux, uy)
        m = len(lst)
        for j in range(m):
            sft = (j - (m - 1) / 2) * 21
            obstacles.append(Point(P[end][0] + ux * 26 - uy * sft, P[end][1] + uy * 26 + ux * sft).buffer(11))

    collisions = 0
    # lanes of different edges overlapping
    tree = STRtree([g for _, _, g in edge_geoms])
    for i, (u, v, g) in enumerate(edge_geoms):
        for j in tree.query(g):
            if j <= i:
                continue
            u2, v2, g2 = edge_geoms[j]
            if {u, v} & {u2, v2}:
                continue
            if g.intersection(g2).area > 6:
                collisions += 1
                scene.dbg['lane'] += 1
    # stations inside the pill
    for n in scene.dist:
        if n != hub and tph.get(n) and Point(P[n]).distance(Point(P[hub])) < hub_half * 1.2:
            collisions += 1
            scene.dbg['pill'] += 1

    placed, labels = [], []
    names = sorted((n for n in scene.dist if n != hub and tph.get(n) and n in stn_name),
                   key=lambda n: -bundle[n])
    for n in names:
        x, y = P[n]
        name = stn_name[n]
        t = fmt_tph(tph[n])
        text_w = 7.4 * (len(name) + len(t) + 1) + 20 * len(end_label.get(n, []))
        half = bundle[n] + 12
        best = None
        tang = scene.node_tangent(n)
        for pref, (mode, poly, tx, ty, anchor, rot) in enumerate(candidates(x, y, half, text_w, tang)):
            cost = pref * 2.0
            for ob in obstacles:
                if ob.intersects(poly):
                    cost += 1000 + ob.intersection(poly).area
            for pl in placed:
                if pl.intersects(poly):
                    cost += 1000 + pl.intersection(poly).area
            if best is None or cost < best[0]:
                best = (cost, mode, poly, tx, ty, anchor, rot)
        cost, mode, poly, tx, ty, anchor, rot = best
        if cost >= 1000:
            collisions += 1
            scene.dbg['label'] += 1
            scene.dbg_names.append((name, round(bundle[n]/PITCH*2)))
        placed.append(poly)
        labels.append((n, tx, ty, anchor, rot, name, t, text_w))
    hname = data.names.get(scene.hub_crs, scene.hub_crs)
    hw = 13.5 * (len(hname) + 5) * 0.8
    best = None
    hx, hy = P[hub]
    for pref, (mode, poly, tx, ty, anchor, rot) in enumerate(candidates(hx, hy, hub_half + 6, hw, (0.0, 1.0))):
        if rot:
            continue
        cost = pref * 2.0
        for ob in obstacles[:-0 or None]:
            if ob is pill:
                continue
            if ob.intersects(poly):
                cost += 1000 + ob.intersection(poly).area
        for pl in placed:
            if pl.intersects(poly):
                cost += 1000 + pl.intersection(poly).area
        if best is None or cost < best[0]:
            best = (cost, tx, ty, anchor, poly)
    hub_label = (best[1], best[2], best[3])
    placed.append(best[4])
    if not with_svg:
        return collisions, None
    return collisions, compose_svg(scene, s, data, colours, paths, dots, labels, end_leaf, end_label,
                                   leaf_pos, tph, placed, hub_half, collisions, hub_label, pills)


def compose_svg(scene, s, data, colours, paths, dots, labels, end_leaf, end_label, leaf_pos, tph,
                placed, hub_half, collisions, hub_label, pills):
    P, hub, routes = scene.pos, scene.hub, scene.routes
    xs = [p[0] for p in P.values()] + [b for q in placed for b in (q.bounds[0], q.bounds[2])]
    ys = [p[1] for p in P.values()] + [b for q in placed for b in (q.bounds[1], q.bounds[3])]
    # only the area actually drawn
    drawn = [pt for _, pc in paths for pt in pc]
    xs = [p[0] for p in drawn] + [b for q in placed for b in (q.bounds[0], q.bounds[2])]
    ys = [p[1] for p in drawn] + [b for q in placed for b in (q.bounds[1], q.bounds[3])]
    x0, x1, y0, y1 = min(xs) - 50, max(xs) + 50, min(ys) - 50, max(ys) + 50
    head, leg_h = 150, 70 + 27 * len(routes)
    W = max(x1 - x0, 840)
    Ht = y1 - y0 + head + leg_h + 40
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s %s %s %s" font-family="Helvetica Neue, '
           'Helvetica, Arial, sans-serif">' % (f(x0), f(y0 - head), f(W), f(Ht))]
    out.append('<rect x="%s" y="%s" width="%s" height="%s" fill="#fff"/>' % (f(x0), f(y0 - head), f(W), f(Ht)))
    hubname = data.names.get(scene.hub_crs, scene.hub_crs)
    out.append('<text x="%s" y="%s" font-size="56" font-weight="300" fill="#c2185b">%s Routes</text>'
               % (f(x0 + 30), f(y0 - head + 70), esc(hubname)))
    out.append('<text x="%s" y="%s" font-size="19" fill="#444">Normal daytime service pattern: average '
               'trains per hour (tph) each way, Tuesday&#8211;Thursday %02d:00&#8211;%02d:00, September 2026</text>'
               % (f(x0 + 32), f(y0 - head + 102), WINDOW[0], WINDOW[1]))
    out.append('<text x="%s" y="%s" font-size="14" fill="#777">Source: National Rail Darwin Push Port, services '
               'that ran. Lines follow the National Rail route map; up to %d calling points each side of %s. '
               'Number beside a station: tph calling; white dot: the route stops.</text>'
               % (f(x0 + 32), f(y0 - head + 126), base.K_STOPS, esc(hubname)))
    for rid, pc in paths:
        # a thin white edge under each line makes any crossing read as over/under
        out.append('<path d="M%s" fill="none" stroke="#fff" stroke-width="%s" stroke-linejoin="round" '
                   'stroke-linecap="butt"/>' % (pts_d(pc), LINE_W + 2.2))
        out.append('<path d="M%s" fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round" '
                   'stroke-linecap="butt"/>' % (pts_d(pc), colours[rid], LINE_W))
    hx, hy = P[hub]
    out.append('<rect x="%s" y="%s" width="%s" height="%s" rx="15" fill="#fff" stroke="#111" stroke-width="3.5"/>'
               % (f(hx - hub_half), f(hy - hub_half), f(2 * hub_half), f(2 * hub_half)))
    for n, (cx, cy, deg, hw) in pills.items():
        out.append('<rect x="%s" y="%s" width="16" height="%s" rx="8" fill="#fff" stroke="#111" '
                   'stroke-width="2.4" transform="rotate(%s %s %s)"/>'
                   % (f(cx - 8), f(cy - hw), f(2 * hw), f(deg), f(cx), f(cy)))
    for _, (x, y), _n in dots:
        out.append('<circle cx="%s" cy="%s" r="3.9" fill="#fff" stroke="#111" stroke-width="1.5"/>' % (f(x), f(y)))
    htx, hty, hanchor = hub_label
    out.append('<text x="%s" y="%s" font-size="22" font-weight="700" fill="#111" text-anchor="%s" '
               'stroke="#fff" stroke-width="5" paint-order="stroke">%s <tspan fill="#c2185b" font-size="15">%s'
               '</tspan></text>' % (f(htx), f(hty), hanchor, esc(hubname),
                                    fmt_tph(sum(r["tph"] for r in routes))))
    for n, tx, ty, anchor, rot, name, t, tw in labels:
        tr = ' transform="rotate(%d %s %s)"' % (rot, f(tx), f(ty)) if rot else ""
        out.append('<text x="%s" y="%s" font-size="%s" fill="#1a1a1a" text-anchor="%s"%s><tspan '
                   'font-weight="500">%s</tspan><tspan dx="6" font-size="12" fill="#c2185b" font-weight="700">%s'
                   '</tspan></text>' % (f(tx), f(ty), FONT, anchor, tr, esc(name), t))
        for j, rid in enumerate(end_label.get(n, [])):
            w = 7.4 * (len(name) + len(t) + 1) + 18 + 20 * j
            a = math.radians(rot)
            sign = 1 if anchor == "start" else -1
            out.append(badge(colours[rid], rid, tx + sign * w * math.cos(a), ty + sign * w * math.sin(a) - 4, 8.5))
    for end, lst in end_leaf.items():
        ux, uy = leaf_pos[end]
        m = len(lst)
        for j, rid in enumerate(lst):
            sft = (j - (m - 1) / 2) * 21
            out.append(badge(colours[rid], rid, P[end][0] + ux * 26 - uy * sft, P[end][1] + uy * 26 + ux * sft, 9.5))
    out.append(legend(routes, colours, data, x0 + 30, y1 + 30))
    out.append("<!-- spacing %.1f collisions %d -->" % (s, collisions))
    out.append("</svg>")
    return "\n".join(out)


def build_map(data, nr, hub_crs, verbose=False):
    routes = discover_routes(data, hub_crs)
    if not routes:
        return None
    scene = Scene(data, nr, hub_crs, routes)
    colours = {r["id"]: PALETTE[i % len(PALETTE)] for i, r in enumerate(routes)}
    results = []
    for s in SCALES:
        c, _ = render(scene, s, data, colours, with_svg=False)
        results.append((s, c))
        if verbose:
            print("   spacing %5.1f collisions %d %s %s" % (s, c, dict(scene.dbg), scene.dbg_names if s == 65 else ""))
    # shrink until the first collision; fall back to the least-bad spacing if the widest already clashes
    tol = max(0, round(0.01 * len(scene.stn)))
    chosen = None
    for s, c in results:
        if c <= tol:
            chosen = s
        else:
            break
    if chosen is None:
        chosen = min(results, key=lambda sc: (sc[1], -sc[0]))[0]
    c, svg = render(scene, chosen, data, colours)
    return svg, scene.routes, chosen, c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stations", nargs="+")
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--stops", type=int, default=7, help="calling points kept each side of the hub")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "maps"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    base.K_STOPS = a.stops
    data = Data(a.patterns, a.tiplocs)
    nr = Network()
    nr.build_anchors(data)
    for crs in a.stations:
        res = build_map(data, nr, crs)
        if res is None:
            print(crs, "no routes")
            continue
        svg, routes, s, c = res
        open(os.path.join(a.out, crs + ".svg"), "w").write(svg)
        print(crs, data.names.get(crs), len(routes), "routes; scale", s, "collisions", c)


if __name__ == "__main__":
    main()
