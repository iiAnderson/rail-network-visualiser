"""Draw a station's routes on a LOOM layout (positions, edge paths, route order) in our own style.

Each route is one smooth path that follows its own lane along the layout's edges (the lane offset eases between
bundles so nothing kinks at a station); a dot is drawn only where the route stops, a capsule only at a major
interchange, and labels are placed clear of lines, dots and each other.
"""
import collections
import json
import math

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box
from shapely.strtree import STRtree
from shapely import affinity

import hub_map as hm
import station_route_map as base
from station_route_map import PALETTE, FONT, LINE_W, PITCH, WINDOW, badge, esc, f, fmt_tph, legend

R_EARTH = 6378137.0
TRANS = 30.0          # px over which a route's lane eases from one bundle position to the next
STEP = 3.0            # px between samples along a route
SPACING_MIN = 120.0   # px per typical hop, at least
DOT_R, MAJOR_TPH = hm.DOT_R, hm.MAJOR_TPH


def project(lon, lat):
    return R_EARTH * math.radians(lon), -R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


class Layout:
    """Nodes and edges of LOOM's octilinear output, in pixels, and each route's walk over them."""

    def __init__(self, octi_json, routes):
        g = octi_json if isinstance(octi_json, dict) else json.loads(octi_json)
        self.nodes, self.edges = {}, []
        raw = []
        for ft in g["features"]:
            p = ft["properties"]
            if ft["geometry"]["type"] == "Point":
                x, y = project(*ft["geometry"]["coordinates"])
                self.nodes[p["id"]] = {"sid": p.get("station_id"), "xy": (x, y)}
            else:
                raw.append((p, [project(*c) for c in ft["geometry"]["coordinates"]]))
        # scale: a typical hop between stations gets SPACING px, wide enough for the widest bundle
        lens = [LineString(pts).length for _, pts in raw if len(pts) > 1]
        widest = max([len(p["lines"]) for p, _ in raw] or [1])
        self.spacing = max(SPACING_MIN, 0.9 * widest * PITCH)
        k = self.spacing / max(float(np.median(lens)) if lens else 1.0, 1e-9)
        self.k = k
        for nid, n in self.nodes.items():
            n["xy"] = (n["xy"][0] * k, n["xy"][1] * k)
        for p, pts in raw:
            order = [l["id"] for l in p["lines"]]
            a, b = self.nodes[p["from"]]["sid"], self.nodes[p["to"]]["sid"]
            # LOOM orders an edge's routes relative to the direction it was given (we give lower code -> higher);
            # its octilinear step may turn the edge round without reversing that order, so put it right here
            if a and b and a > b:
                order = order[::-1]
            self.edges.append({"from": p["from"], "to": p["to"], "pts": [(x * k, y * k) for x, y in pts],
                               "order": order})
        self.node_of = {n["sid"]: nid for nid, n in self.nodes.items() if n["sid"]}
        self.routes = routes
        self._walks()

    def _walks(self):
        """For every route, the list of (edge, forward) it follows from its first to its last station."""
        by_route = collections.defaultdict(lambda: collections.defaultdict(list))
        for ei, e in enumerate(self.edges):
            for rid in e["order"]:
                by_route[rid][e["from"]].append((ei, e["to"], True))
                by_route[rid][e["to"]].append((ei, e["from"], False))
        self.walk = {}
        for r in self.routes:
            adj = by_route[r["id"]]
            st = [c for c in r["path"] if c in self.node_of]
            seq = [st[0]] if st else []
            for c in st[1:]:
                if c != seq[-1]:
                    seq.append(c)
            legs = []
            for a, b in zip(seq, seq[1:]):
                na, nb = self.node_of[a], self.node_of[b]
                prev, todo = {na: None}, collections.deque([na])
                while todo and nb not in prev:
                    u = todo.popleft()
                    for ei, v, fwd in adj[u]:
                        if v not in prev:
                            prev[v] = (u, ei, fwd)
                            todo.append(v)
                if nb not in prev:
                    continue
                path, v = [], nb
                while prev[v] is not None:
                    u, ei, fwd = prev[v]
                    path.append((ei, fwd))
                    v = u
                legs += path[::-1]
            self.walk[r["id"]] = (seq, legs)

    def lane(self, edge, rid):
        order = edge["order"]
        return order.index(rid) - (len(order) - 1) / 2.0


def route_geometry(L, r, sign):
    """Centreline samples of one route, its lateral offset along them, and where its stops fall."""
    seq, legs = L.walk[r["id"]]
    if not legs:
        return None
    pts, lat, bounds, forward = [], [], [], []      # bounds: sample index where each leg starts
    for ei, fwd in legs:
        e = L.edges[ei]
        ep = e["pts"] if fwd else e["pts"][::-1]
        if pts and math.dist(pts[-1], ep[0]) < 1e-6:
            bounds.append(len(pts) - 1)     # the leg starts at the vertex the previous one ended on
            ep = ep[1:]
        else:
            bounds.append(len(pts))
        pts += ep
        forward.append(fwd)
    bounds.append(len(pts) - 1)
    # lateral value per leg, in the frame of travel
    val = []
    for (ei, fwd) in legs:
        d = sign * L.lane(L.edges[ei], r["id"]) * PITCH
        val.append(d if fwd else -d)
    # resample the centreline, keeping the arc length of every vertex
    P = np.array(pts)
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    S = np.concatenate([[0.0], np.cumsum(seg)])
    n = max(int(S[-1] / STEP) + 1, 2)
    ss = np.linspace(0.0, S[-1], n)
    X = np.interp(ss, S, P[:, 0])
    Y = np.interp(ss, S, P[:, 1])
    # lateral: constant per leg, easing to the mean of neighbouring legs near each node
    legS = [S[b] for b in bounds]
    lat = np.zeros(n)
    for i, v in enumerate(val):
        a, b = legS[i], legS[i + 1]
        t = min(TRANS, 0.45 * (b - a))
        v0 = (val[i - 1] + v) / 2 if i else v
        v1 = (v + val[i + 1]) / 2 if i + 1 < len(val) else v
        m = (ss >= a - 1e-9) & (ss <= b + 1e-9)
        s = ss[m]
        out = np.full(s.shape, v)
        if t > 1e-6:
            out = np.where(s - a < t, v0 + (v - v0) * (s - a) / t, out)
            out = np.where(b - s < t, v1 + (v - v1) * (b - s) / t, out)
        lat[m] = out
    # unit tangent and left normal (central differences)
    dx = np.gradient(X)
    dy = np.gradient(Y)
    nrm = np.hypot(dx, dy) + 1e-12
    tx, ty = dx / nrm, dy / nrm
    nx, ny = ty, -tx
    OX, OY = X + nx * lat, Y + ny * lat
    # stop positions: the sample at each station node's arc length
    stops = {}
    seqn = [L.node_of[c] for c in seq]
    pos_at_node = {}
    for i, (ei, fwd) in enumerate(legs):
        e = L.edges[ei]
        pos_at_node.setdefault(e["from"] if fwd else e["to"], legS[i])
        pos_at_node[e["to"] if fwd else e["from"]] = legS[i + 1]
    for c, nid in zip(seq, seqn):
        if nid in pos_at_node:
            j = int(np.argmin(np.abs(ss - pos_at_node[nid])))
            stops[c] = (float(OX[j]), float(OY[j]), float(tx[j]), float(ty[j]))
    jump = sum(abs(val[i] - val[i + 1]) for i in range(len(val) - 1))
    return {"xy": np.column_stack([OX, OY]), "stops": stops, "jump": jump}


def pick_sign(L):
    """LOOM lists the routes of an edge in order; whether that is left-to-right or right-to-left of the edge's
    direction is not documented, so choose the one whose lanes change least from edge to edge."""
    best = None
    for sign in (1, -1):
        j = 0.0
        for r in L.routes:
            seq, legs = L.walk[r["id"]]
            val = []
            for ei, fwd in legs:
                d = sign * L.lane(L.edges[ei], r["id"]) * PITCH
                val.append(d if fwd else -d)
            j += sum(abs(val[i] - val[i + 1]) for i in range(len(val) - 1))
        if best is None or j < best[0]:
            best = (j, sign)
    return best[1]


def render(data, hub, routes, octi_json, title_suffix=""):
    L = Layout(octi_json, routes)
    sign = pick_sign(L)
    colours = {r["id"]: PALETTE[i % len(PALETTE)] for i, r in enumerate(routes)}
    geom = {}
    for r in routes:
        g = route_geometry(L, r, sign)
        if g:
            geom[r["id"]] = g
    # ---- stops: who calls where, tph, interchanges
    calling = collections.defaultdict(list)       # station -> routes that stop there (and draw)
    for r in routes:
        g = geom.get(r["id"])
        if not g:
            continue
        for c in g["stops"]:
            if c in r["calls"] or c == hub:
                calling[c].append(r)
    tph = {c: sum(r["tph"] for r in rs if r.get("calls") is None or c in r["calls"]) for c, rs in calling.items()}
    ends = collections.defaultdict(list)
    for r in routes:
        seq, _ = L.walk[r["id"]]
        if r["id"] in geom and seq:
            ends[seq[0]].append(r["id"])
            if seq[-1] != seq[0]:
                ends[seq[-1]].append(r["id"])
    # neighbours of a route at a station, to decide whether routes part company there
    def way(r, c):
        p = r["path"]
        i = p.index(c)
        return frozenset([p[i - 1] if i else None, p[i + 1] if i + 1 < len(p) else None])
    capsule = {}
    for c, rs in calling.items():
        pts = [geom[r["id"]]["stops"][c][:2] for r in rs]
        if c == hub or (tph[c] >= MAJOR_TPH and len({way(r, c) for r in rs}) >= 2 and len(rs) >= 2):
            capsule[c] = pts
    # ---- geometry for collision tests
    lines = [LineString(g["xy"]) for g in geom.values()]
    allpts = [(x, y) for g in geom.values() for x, y in g["xy"]]
    xs = [p[0] for p in allpts]
    ys = [p[1] for p in allpts]
    dots = [(rid, c, g["stops"][c][:2]) for rid, g in geom.items() for c in g["stops"]
            if c in calling and any(rr["id"] == rid for rr in calling[c])]
    obstacles = [ln.buffer(LINE_W / 2 + 1.5) for ln in lines] + [Point(p).buffer(DOT_R + 2) for _, _, p in dots]
    tree = STRtree(obstacles)
    placed, labels = [], []
    hubname = data.names.get(hub, hub)
    order = sorted(calling, key=lambda c: (c != hub, -tph[c]))
    for c in order:
        ps = [geom[r["id"]]["stops"][c] for r in calling[c]]
        cx, cy = sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps)
        half = max([math.dist((p[0], p[1]), (cx, cy)) for p in ps] + [DOT_R]) + DOT_R + 4
        name = data.names.get(c, c)
        big = c == hub
        size = 22 if big else FONT
        wd = hm.text_w(name, size, bold=big or c in ends) + 6 + hm.text_w(fmt_tph(tph[c]), 12, True)
        best = None
        cands = []
        for dist in (0, 10, 22):
            for ang, anchor in ((0, "start"), (180, "end"), (-35, "start"), (35, "start"), (-145, "end"), (145, "end"),
                                (-90, "start"), (90, "start")):
                cands.append((dist, ang, anchor))
        for k, (dist, ang, anchor) in enumerate(cands):
            a = math.radians(ang)
            ox, oy = (half + dist) * math.cos(a), (half + dist) * math.sin(a)
            tx, ty = cx + ox, cy + oy + (4 if ang in (0, 180) else 0)
            rot = ang if anchor == "start" and ang not in (0, 180) else (ang - 180 if ang not in (0, 180) else 0)
            if abs(ang) == 90:
                rot = -90 if ang == -90 else 90
            hh = size
            poly = box(0, -hh * 0.8, wd, hh * 0.25) if anchor == "start" else box(-wd, -hh * 0.8, 0, hh * 0.25)
            if rot:
                poly = affinity.rotate(poly, rot, origin=(0, 0))
            poly = affinity.translate(poly, tx, ty)
            hit = sum(poly.intersection(obstacles[i]).area for i in tree.query(poly) if poly.intersects(obstacles[i]))
            hit += sum(poly.intersection(q).area for q in placed if poly.intersects(q))
            score = hit * 10 + k * 0.4
            if best is None or score < best[0]:
                best = (score, tx, ty, anchor, rot, poly, hit)
            if hit == 0:
                break
        _, tx, ty, anchor, rot, poly, hit = best
        placed.append(poly)
        labels.append((c, tx, ty, anchor, rot, name, fmt_tph(tph[c]), big, hit > 0))
    # ---- svg
    x0, x1 = min(xs) - 120, max(xs) + 120
    y0, y1 = min(ys) - 80, max(ys) + 80
    for q in placed:
        b = q.bounds
        x0, x1, y0, y1 = min(x0, b[0] - 20), max(x1, b[2] + 20), min(y0, b[1] - 20), max(y1, b[3] + 20)
    head, leg_h = 150, 70 + 27 * len(routes)
    W = max(x1 - x0, 840)
    H = y1 - y0 + head + leg_h + 40
    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s %s %s %s" font-family="Helvetica Neue, Helvetica, Arial, '
         'sans-serif">' % (f(x0), f(y0 - head), f(W), f(H)),
         '<rect x="%s" y="%s" width="%s" height="%s" fill="#fff"/>' % (f(x0), f(y0 - head), f(W), f(H)),
         '<text x="%s" y="%s" font-size="56" font-weight="300" fill="#c2185b">%s Routes%s</text>'
         % (f(x0 + 30), f(y0 - head + 70), esc(hubname), title_suffix),
         '<text x="%s" y="%s" font-size="19" fill="#444">Normal daytime service pattern: average trains per hour (tph) '
         'each way, Tue&#8211;Thu %02d:00&#8211;%02d:00, median of Jun&#8211;Oct 2026</text>'
         % (f(x0 + 32), f(y0 - head + 102), WINDOW[0], WINDOW[1]),
         '<text x="%s" y="%s" font-size="14" fill="#777">Source: National Rail Darwin Push Port. Network layout: stations '
         'keep their true bearing from the hub; distance is compressed.</text>' % (f(x0 + 32), f(y0 - head + 126)),
         '<text x="%s" y="%s" font-size="14" fill="#777">Number beside a station: tph calling. White dot: the route '
         'stops. Capsule: major interchange between routes going different ways.</text>' % (f(x0 + 32), f(y0 - head + 145))]
    for rid, g in geom.items():
        d = "M" + " L".join("%s,%s" % (f(x), f(y)) for x, y in g["xy"][::2].tolist() + [g["xy"][-1].tolist()])
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round" stroke-linecap="round"/>'
                 % (d, colours[rid], LINE_W))
    for c, pts in capsule.items():
        P = np.array(pts)
        if len(P) >= 2:
            cen = P.mean(axis=0)
            u, s, vt = np.linalg.svd(P - cen)
            ax = vt[0]
            ext = float(np.max(np.abs((P - cen) @ ax))) + 9
            deg = math.degrees(math.atan2(ax[1], ax[0])) - 90
        else:
            cen, ext, deg = P[0], 9, 0
        o.append('<rect x="%s" y="%s" width="16" height="%s" rx="8" fill="#fff" stroke="#111" stroke-width="2.4" '
                 'transform="rotate(%s %s %s)"/>' % (f(cen[0] - 8), f(cen[1] - ext), f(2 * ext), f(deg), f(cen[0]), f(cen[1])))
    for rid, c, (x, y) in dots:
        o.append('<circle cx="%s" cy="%s" r="%s" fill="#fff" stroke="#111" stroke-width="1.5"/>' % (f(x), f(y), DOT_R))
    for c, tx, ty, anchor, rot, name, t, big, clash in labels:
        tr = ' transform="rotate(%d %s %s)"' % (rot, f(tx), f(ty)) if rot else ""
        weight = "700" if (big or c in ends) else "500"
        o.append('<text x="%s" y="%s" font-size="%s" fill="#1a1a1a" text-anchor="%s" stroke="#fff" stroke-width="4" '
                 'stroke-linejoin="round" paint-order="stroke"%s><tspan font-weight="%s">%s</tspan>'
                 '<tspan dx="6" font-size="12" fill="#c2185b" font-weight="700">%s</tspan></text>'
                 % (f(tx), f(ty), 22 if big else FONT, anchor, tr, weight, esc(name), t))
    for c, rids in ends.items():
        for j, rid in enumerate(rids):
            g = geom[rid]
            if c not in g["stops"]:
                continue
            x, y, tx_, ty_ = g["stops"][c]
            seq, _ = L.walk[rid]
            sgn = -1 if c == seq[0] else 1        # outward: backwards from the first stop, forwards from the last
            o.append(badge(colours[rid], rid, x + sgn * tx_ * (26 + 21 * j), y + sgn * ty_ * (26 + 21 * j), 9.5))
    o.append(legend(routes, colours, data, x0 + 30, y1 + 30))
    clashes = sum(1 for lb in labels if lb[-1])
    report = {"routes": len(routes), "stations": len(calling), "dots": len(dots), "label_clash": clashes,
              "sign": sign}
    o.append("<!-- network layout report %s -->" % report)
    o.append("</svg>")
    return "\n".join(o), report
