"""Avanti-style route map centred on any station, built automatically from service patterns.

    poetry run python3 station_route_map.py RDG DON BRI \
        --patterns national_patterns.csv --tiplocs tiplocs_geo.csv --out ../maps

Pipeline: services calling at the hub -> routes (stopping patterns clustered by overlap,
tph = trains per hour each way) -> station graph -> tree rooted at the hub -> octilinear
branch directions from real bearings -> lane order from the tree (so lanes only split where
routes diverge) -> greedy label placement -> SVG.
"""
import argparse
import collections
import heapq
import json
import math
import os
import sys

import pandas as pd
from shapely.geometry import LineString, Point, Polygon, box

WINDOW = (9, 16)        # local hour at the hub; avoids peaks and early/late services
K_STOPS = 10            # keep this many calling points either side of the hub
MIN_TPH = 0.3           # drop routes rarer than this (trains per hour each way)
MAX_ROUTES = 24
MIN_ON_ROUTE = 0.8      # share of a service's stops that must lie on a route to join it
MIN_JACCARD = 0.5

PITCH, LINE_W, CORNER_R, TRANSITION = 8.0, 5.4, 18.0, 26.0
SPACING = 60.0
FONT = 14.5

PALETTE = ["#0a6e3d", "#d62828", "#1b4f9c", "#f28c00", "#7b4fa3", "#00a5b5", "#c2185b",
           "#8b5a2b", "#1d1d2b", "#9ccc3c", "#5aa9e6", "#e6b800", "#e57373", "#7f8c8d",
           "#3d9970", "#b10dc9", "#ff6f91", "#4b6584", "#a3cb38", "#0652dd", "#ea2027",
           "#833471", "#12cbc4", "#a5764a"]
EXTRA_NAMES = {"PTWYPR": "Portway Park & Ride", "ASHYDN": "Ashley Down",
               "WORCPWY": "Worcestershire Parkway"}


# ------------------------------------------------------------------------ data
class Data:
    def __init__(self, patterns_csv, tiplocs_csv):
        t = pd.read_csv(tiplocs_csv, dtype=str).drop_duplicates("TiplocCode")
        self.crs_of = dict(zip(t.TiplocCode, t.CrsCode))
        self.names = {c: n.replace(" Rail Station", "") for c, n in zip(t.CrsCode, t.StationName)}
        self.xy = {}
        for c, e, n in zip(t.CrsCode, t.easting, t.northing):
            try:
                self.xy[c] = (float(e), -float(n))  # y down so north is up
            except (TypeError, ValueError):
                pass
        cache = patterns_csv + ".pkl"
        if os.path.exists(cache) and os.path.getmtime(cache) > os.path.getmtime(patterns_csv):
            import pickle
            self.days, self.services, extra = pickle.load(open(cache, "rb"))
            self.names.update(extra)
            for tp, nm in EXTRA_NAMES.items():
                self.names[self.crs_of.get(tp, tp)] = nm
            return
        d = pd.read_csv(patterns_csv, dtype=str).dropna(subset=["pattern"])
        self.days = d.service_date.nunique()
        self.services = []
        for toc, pat, tm in zip(d.toc, d.pattern, d["times"]):
            stops, times = [], []
            for tp, tt in zip(pat.split(","), tm.split(",")):
                c = self.crs_of.get(tp, tp)
                if not stops or stops[-1] != c:
                    stops.append(c)
                    times.append(tt)
                    if c not in self.names:
                        self.names[c] = EXTRA_NAMES.get(tp, tp.title())
            self.services.append((toc, tuple(stops), tuple(times)))
        for tp, nm in EXTRA_NAMES.items():
            self.names[self.crs_of.get(tp, tp)] = nm
        import pickle
        pickle.dump((self.days, self.services, {k: v for k, v in self.names.items()}), open(cache, "wb"))


# ------------------------------------------------------------------------ routes
def discover_routes(data, hub):
    slots = data.days * (WINDOW[1] - WINDOW[0])
    counts = collections.Counter()
    ends = collections.defaultdict(collections.Counter)
    for toc, stops, times in data.services:
        if hub not in stops:
            continue
        i = stops.index(hub)
        if not WINDOW[0] <= int(times[i][:2]) < WINDOW[1]:
            continue
        lo, hi = max(0, i - K_STOPS), min(len(stops), i + K_STOPS + 1)
        cut = stops[lo:hi]
        key = (toc, cut if cut <= cut[::-1] else cut[::-1])
        counts[key] += 1
        ends[key][(stops[0], stops[-1])] += 1

    clusters = []  # each: dict(toc, seq, set, n, ends)
    for (toc, seq), n in counts.most_common():
        s = set(seq)
        best, bj = None, 0.0
        for c in clusters:
            if c["toc"] != toc:
                continue
            if len(s & c["set"]) / len(s) < MIN_ON_ROUTE:
                continue
            j = len(s & c["set"]) / len(s | c["set"])
            if j > bj:
                best, bj = c, j
        if best is not None and bj >= MIN_JACCARD:
            best["n"] += n
            best["ends"].update(ends[(toc, seq)])
            for st in seq:
                best["calls"][st] += n
        else:
            clusters.append({"toc": toc, "seq": list(seq), "set": s, "n": n,
                             "ends": collections.Counter(ends[(toc, seq)]),
                             "calls": collections.Counter({st: n for st in seq})})
    routes = []
    for c in sorted(clusters, key=lambda c: -c["n"]):
        tph = c["n"] / slots / 2
        if tph < MIN_TPH or len(routes) >= MAX_ROUTES:
            continue
        # a route that is truncated: remember where it really goes
        far = c["ends"].most_common(1)[0][0]
        routes.append({"toc": c["toc"], "seq": c["seq"], "tph": tph,
                       "calls": {s for s in c["seq"] if c["calls"][s] / c["n"] >= 0.5},
                       "true_ends": far})
    letters = [chr(65 + i) if i < 26 else str(i) for i in range(len(routes))]
    for r, l in zip(routes, letters):
        r["id"] = l
    return routes


XY = {}


def expand_skips(routes):
    """Replace an edge that skips stations with the finer chain another route provides."""
    seqs = [list(r["seq"]) for r in routes]
    originals = [list(r["seq"]) for r in routes]

    def chain(a, c, exclude):
        for k, other in enumerate(originals):
            if k == exclude:
                continue
            if a in other and c in other:
                i, j = other.index(a), other.index(c)
                if abs(i - j) > 1:
                    sub = other[i:j + 1] if i < j else other[j:i + 1][::-1]
                    if len(set(sub)) == len(sub):
                        return sub
        return None

    for _ in range(6):
        changed = False
        for k, seq in enumerate(seqs):
            out = [seq[0]]
            for c in seq[1:]:
                sub = chain(out[-1], c, k)
                if sub and not (set(sub[1:-1]) & set(out)) and len(out) < 150:
                    out += sub[1:]
                    changed = True
                else:
                    out.append(c)
            seqs[k] = out
        if not changed:
            break
    # geometric fallback: a remaining skip edge picks up stations other routes call at that
    # lie along it (e.g. a fast and a semi-fast service skipping different stops)
    for _ in range(3):
        grew = False
        # candidates for route k: stations of routes that share >= 2 stations with it
        cand_for = []
        for k, seq in enumerate(seqs):
            cand = set()
            for k2, other in enumerate(originals):
                if k2 != k:
                    cand |= set(other)
            cand_for.append(cand)
        for k, seq in enumerate(seqs):
            have = set(seq)
            out = [seq[0]]
            for c in seq[1:]:
                a = out[-1]
                if a in XY and c in XY:
                    ax, ay = XY[a]
                    cx, cy = XY[c]
                    L2 = (cx - ax) ** 2 + (cy - ay) ** 2
                    L = math.sqrt(L2)
                    found = []
                    if L > 4000:
                        for n in cand_for[k] - have:
                            if n not in XY:
                                continue
                            t = ((XY[n][0] - ax) * (cx - ax) + (XY[n][1] - ay) * (cy - ay)) / L2
                            perp = abs((XY[n][0] - ax) * (cy - ay) - (XY[n][1] - ay) * (cx - ax)) / L
                            if 0.04 < t < 0.96 and perp < max(1000, 0.02 * L):
                                found.append((t, n))
                    for _, n in sorted(found):
                        out.append(n)
                        have.add(n)
                        grew = True
                out.append(c)
            seqs[k] = out
        if not grew:
            break
        for _ in range(4):
            for k, seq in enumerate(seqs):
                out = [seq[0]]
                for c in seq[1:]:
                    sub = chain(out[-1], c, k)
                    ok = sub and not (set(sub[1:-1]) & set(out)) and len(out) < 150
                    out += sub[1:] if ok else [c]
                seqs[k] = out
    for r, s in zip(routes, seqs):
        r["path"] = s


# ------------------------------------------------------------------------ tree + layout
def build_tree(routes, hub):
    """Spanning tree that keeps the most traffic: where the network has loops (two corridors
    between the same places) the busier corridor stays in the tree and the quieter one hangs off
    it as a spur."""
    traffic = collections.Counter()
    adj = collections.defaultdict(set)
    for r in routes:
        for a, b in zip(r["path"], r["path"][1:]):
            if a != b:
                adj[a].add(b)
                adj[b].add(a)
                traffic[frozenset((a, b))] += r["tph"]
    root = {}

    def find(x):
        while root.setdefault(x, x) != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    keep = collections.defaultdict(set)
    for e in sorted(traffic, key=lambda e: -traffic[e]):
        a, b = tuple(e)
        ra, rb = find(a), find(b)
        if ra != rb:
            root[ra] = rb
            keep[a].add(b)
            keep[b].add(a)
    parent = {hub: None}
    q = collections.deque([hub])
    while q:
        u = q.popleft()
        for v in sorted(keep[u], key=lambda v: -traffic[frozenset((u, v))]):
            if v not in parent:
                parent[v] = u
                q.append(v)
    children = {v: [] for v in parent}
    for v, p in parent.items():
        if p is not None:
            children[p].append(v)
    return parent, children


def bearing_vec(u, kids_of, xy, node, depth=4):
    """Mean unit vector from `node` to the known stations in its subtree (<= depth)."""
    if node not in xy:
        return None
    vx = vy = 0.0
    stack = [(c, 1) for c in kids_of[node]]
    while stack:
        c, dp = stack.pop()
        if c in xy:
            dx, dy = xy[c][0] - xy[node][0], xy[c][1] - xy[node][1]
            n = math.hypot(dx, dy) or 1
            vx += dx / n
            vy += dy / n
        if dp < depth:
            stack += [(g, dp + 1) for g in kids_of[c]]
    return (vx, vy) if (vx or vy) else None


def ang(v):
    return math.atan2(v[1], v[0])


def angdiff(a, b):
    d = (a - b + math.pi) % (2 * math.pi) - math.pi
    return abs(d)


def layout(routes, hub, data):
    parent, children = build_tree(routes, hub)
    xy = data.xy
    # the vector from a node to a *child's own subtree* (including the child)
    sub_vec = {}
    for p, kids in list(children.items()):
        for c in kids:
            if p in xy:
                vx = vy = 0.0
                stack = [(c, 1)]
                while stack:
                    n, dp = stack.pop()
                    if n in xy:
                        dx, dy = xy[n][0] - xy[p][0], xy[n][1] - xy[p][1]
                        m = math.hypot(dx, dy) or 1
                        vx, vy = vx + dx / m, vy + dy / m
                    if dp < 4:
                        stack += [(g, dp + 1) for g in children[n]]
                sub_vec[c] = (vx, vy) if (vx or vy) else None

    OCT = [k * math.pi / 4 for k in range(8)]
    dir_of = {}  # tree edge child -> direction angle (outward)
    order = {}   # node -> children left-to-right facing outward

    def assign(u, theta_in):
        kids = children[u]
        if not kids:
            return
        if theta_in is None:
            allowed = OCT[:]
        else:
            allowed = [theta_in + k * math.pi / 4 for k in (-2, -1, 0, 1, 2)]
        want = {}
        for c in kids:
            v = sub_vec.get(c)
            want[c] = ang(v) if v else (theta_in if theta_in is not None else 0.0)
        free = list(allowed)
        taken = {}
        pairs = sorted(((angdiff(want[c], a) + (0.25 * angdiff(a, theta_in) if theta_in is not None else 0), c, a)
                        for c in kids for a in allowed))
        for _, c, a in pairs:
            if c in taken or a not in free:
                continue
            taken[c] = a
            free.remove(a)
        for c in kids:  # more children than directions: share the closest
            if c not in taken:
                taken[c] = min(allowed, key=lambda a: angdiff(want[c], a))
        for c, a in taken.items():
            dir_of[c] = a
        if theta_in is None:
            order[u] = sorted(kids, key=lambda c: dir_of[c] % (2 * math.pi))
        else:
            # left first when facing theta_in (SVG y down: left normal is (dy,-dx))
            order[u] = sorted(kids, key=lambda c: -math.sin(-(dir_of[c] - theta_in)))
        for c in kids:
            assign(c, dir_of[c])

    assign(hub, None)

    # geometry: station positions plus junction waypoints
    P = {hub: (0.0, 0.0)}
    junction = {}
    draw_children = collections.defaultdict(list)  # draw node -> draw children (outward)
    edge_path = {}  # tree edge (p, c) -> draw node list p..c
    in_theta = {hub: None}
    n_lanes = lanes_per_tree_edge(routes, parent)
    hubn = max([n_lanes.get((hub, c), 1) for c in children[hub]] or [1])
    hub_r = hubn * PITCH / 2 + 10

    def place(u):
        th_in = in_theta[u]
        kids = order.get(u, [])
        base = u
        pend = []
        if u != hub and kids:
            straight_only = len(kids) == 1 and abs(((dir_of[kids[0]] - th_in) + math.pi) % (2 * math.pi) - math.pi) < 1e-6
            if not straight_only:
                nl = max(n_lanes.get((parent[u], u), 1), 1)
                J = 34 + 2.5 * nl
                j = "j:" + u
                P[j] = (P[u][0] + math.cos(th_in) * J, P[u][1] + math.sin(th_in) * J)
                junction[u] = j
                base = j
        for c in kids:
            th = dir_of[c]
            nl = n_lanes.get((u, c), 1)
            if u == hub:
                dist_ = hub_r + 80 + 2 * nl
            elif base != u:
                dist_ = 44 + 2.5 * nl
            else:
                dist_ = SPACING
            P[c] = (P[base][0] + math.cos(th) * dist_, P[base][1] + math.sin(th) * dist_)
            edge_path[(u, c)] = [u] + ([base] if base != u else []) + [c]
            in_theta[c] = th
            place(c)

    place(hub)
    return {"parent": parent, "children": children, "order": order, "P": P, "dir": dir_of,
            "edge_path": edge_path, "junction": junction, "hub_r": hub_r}


def lanes_per_tree_edge(routes, parent):
    cnt = collections.Counter()
    for r in routes:
        for e in set(tree_edges_of(r["path"], parent)):
            cnt[e] += 1
    return cnt


def tree_route(a, b, parent):
    """Tree path from a to b as a node list."""
    up_a, up_b = [a], [b]
    seen = {a}
    while parent[up_a[-1]] is not None:
        up_a.append(parent[up_a[-1]])
        seen.add(up_a[-1])
    while up_b[-1] not in seen:
        up_b.append(parent[up_b[-1]])
    meet = up_b[-1]
    return up_a[: up_a.index(meet) + 1] + up_b[:-1][::-1]


def route_tree_nodes(path, parent):
    out = [path[0]]
    for a, b in zip(path, path[1:]):
        if a == b:
            continue
        out += tree_route(a, b, parent)[1:]
    return out


def tree_edges_of(path, parent):
    nodes = route_tree_nodes(path, parent)
    edges = []
    for a, b in zip(nodes, nodes[1:]):
        edges.append((a, b) if parent.get(b) == a else (b, a))
    return edges


# ------------------------------------------------------------------------ lanes + render
def unit(p, q):
    dx, dy = q[0] - p[0], q[1] - p[1]
    n = math.hypot(dx, dy) or 1
    return dx / n, dy / n


def add(p, q, k=1.0):
    return (p[0] + k * q[0], p[1] + k * q[1])


def intersect(p1, d1, p2, d2):
    den = d1[0] * d2[1] - d1[1] * d2[0]
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
    return add(p1, d1, t)


def f(v):
    return "%.1f" % v


def fmt_tph(v):
    s = "%.1f" % v
    return s[:-2] if s.endswith(".0") else s


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;")


def build_map(data, hub):
    routes = discover_routes(data, hub)
    if not routes:
        return None
    XY.clear()
    XY.update(data.xy)
    expand_skips(routes)
    L = layout(routes, hub, data)
    parent, children, P = L["parent"], L["children"], L["P"]
    hub_r = L["hub_r"]

    # route -> draw node list
    for r in routes:
        tn = route_tree_nodes(r["path"], parent)
        dn = [tn[0]]
        for a, b in zip(tn, tn[1:]):
            if parent.get(b) == a:
                seg = L["edge_path"][(a, b)]
            else:
                seg = L["edge_path"][(b, a)][::-1]
            dn += seg[1:]
        r["draw"] = dn
        r["tree_nodes"] = tn

    # subtree membership and pre-order (children sorted left-to-right)
    pre, subtree, depth = {}, {}, {hub: 0}

    def dfs(u):
        pre[u] = len(pre)
        for c in children[u]:
            depth[c] = depth[u] + 1
        members = {u}
        for c in L["order"].get(u, []):
            members |= dfs(c)
        subtree[u] = members
        return members

    dfs(hub)

    # lanes on every draw edge (canonical outward direction)
    canon = {}  # draw edge (a,b) outward
    tree_of_draw = {}
    for (p, c), seq in L["edge_path"].items():
        for a, b in zip(seq, seq[1:]):
            canon[(a, b)] = (a, b)
            canon[(b, a)] = (a, b)
            tree_of_draw[(a, b)] = (a, c) if b == "j:" + a else (None, c)
    lanes = {}
    for ab, (shared, c) in tree_of_draw.items():
        members = (subtree[shared] - {shared}) if shared else subtree[c]
        users = []
        for r in routes:
            dr = r["draw"]
            if any({dr[i], dr[i + 1]} == set(ab) for i in range(len(dr) - 1)):
                if True:
                    inside = [n for n in r["tree_nodes"] if n in members]
                    if not inside:
                        continue
                    z = max(inside, key=lambda n: depth[n])
                    users.append((pre[z], r["id"]))
        lanes[ab] = [rid for _, rid in sorted(users)]

    def lane(rid, a, b):
        ab = canon[(a, b)]
        order = lanes[ab]
        if rid not in order:  # route backtracks over a non-tree edge: park it at the edge
            order = order + [rid]
        idx = order.index(rid)
        d = unit(P[ab[0]], P[ab[1]])
        return d, ((len(order) - 1) / 2 - idx) * PITCH

    def off(rid, a, b):
        d, s = lane(rid, a, b)
        return (d[1] * s, -d[0] * s)

    # ---- paths
    colours = {r["id"]: PALETTE[i % len(PALETTE)] for i, r in enumerate(routes)}
    svg_paths, dots = [], []
    end_leaf, end_label = collections.defaultdict(list), collections.defaultdict(list)
    for r in routes:
        dn = r["draw"]
        k = dn.index(hub)
        for part in (dn[: k + 1], dn[k:]):
            if len(part) >= 2:
                svg_paths.append((r["id"], path_d(r["id"], part, off, P)))
        r["truncated"] = False
        for end, nxt, true_end in ((dn[0], dn[1], r["true_ends"][0]), (dn[-1], dn[-2], r["true_ends"][1])):
            pass
        for gi, (end, nxt) in enumerate(((dn[0], dn[1]), (dn[-1], dn[-2]))):
            if end == hub:
                continue
            node_is_leaf = not children.get(end)
            if node_is_leaf:
                end_leaf[end].append((r["id"], unit(P[nxt], P[end])))
            else:
                end_label[end].append(r["id"])
        for i, n in enumerate(dn):
            if n in r["calls"] and n != hub and not n.startswith("j:"):
                a, b = (dn[i - 1], n) if i else (n, dn[1])
                dots.append((r["id"], add(P[n], off(r["id"], a, b))))

    # ---- stats
    tph = collections.defaultdict(float)
    for r in routes:
        for c in r["calls"]:
            tph[c] += r["tph"]

    def fmt(v):
        s = "%.1f" % v
        return s[:-2] if s.endswith(".0") else s

    # "continues to" at truncated ends
    cont = {}
    for r in routes:
        fo, fd = r["true_ends"]
        seq = r["seq"]
        for end in (seq[0], seq[-1]):
            if end == hub:
                continue
            for far in (fo, fd):
                if far != end and far != hub and end not in (fo, fd):
                    # the end station is not the true terminus: record the likely destination
                    cont.setdefault(end, data.names.get(far, far))

    # ---- obstacles for label placement
    bundle_w = collections.defaultdict(float)
    obstacles = []
    for ab, order in lanes.items():
        w = len(order) * PITCH / 2 + 3
        obstacles.append(LineString([P[ab[0]], P[ab[1]]]).buffer(w, cap_style=2))
        for n in ab:
            bundle_w[n] = max(bundle_w[n], len(order) * PITCH / 2)
    obstacles.append(Point(P[hub]).buffer(hub_r * 1.45))
    for _, (x, y) in dots:
        obstacles.append(Point(x, y).buffer(5))
    for end, lst in end_leaf.items():
        ux, uy = lst[0][1]
        for j, _ in enumerate(lst):
            m = len(lst)
            s = (j - (m - 1) / 2) * 21
            obstacles.append(Point(P[end][0] + ux * 26 - uy * s, P[end][1] + uy * 26 + ux * s).buffer(11))

    placed = []
    label_out = []
    stations = [n for n in P if not n.startswith("j:") and n != hub and tph.get(n)]
    # place the busiest/most constrained first (nodes with the widest bundles)
    stations.sort(key=lambda n: -bundle_w[n])
    for n in stations:
        x, y = P[n]
        name = data.names.get(n, n)
        suffix = ("  → " + cont[n]) if n in cont else ""
        text_w = 7.5 * (len(name) + len(fmt(tph[n])) + 1 + len(suffix)) * 0.97 + (22 * len(end_label.get(n, [])))
        h = 16.0
        half = bundle_w[n] + 12
        cands = []
        for mode in ("R", "L", "U", "D", "U2", "D2"):
            cands.append(label_candidate(mode, x, y, half, text_w, h))
        best = None
        for pref, (mode, poly, tx, ty, anchor, rot) in enumerate(cands):
            cost = pref * 2.0
            for ob in obstacles:
                if ob.intersects(poly):
                    cost += 1000 + ob.intersection(poly).area
            for pl in placed:
                if pl.intersects(poly):
                    cost += 1000 + pl.intersection(poly).area
            if best is None or cost < best[0]:
                best = (cost, mode, poly, tx, ty, anchor, rot)
        _, mode, poly, tx, ty, anchor, rot = best
        placed.append(poly)
        label_out.append((n, mode, tx, ty, anchor, rot, name, fmt(tph[n]), suffix, text_w))

    # ---- svg
    xs = [p[0] for p in P.values()] + [q.bounds[0] for q in placed] + [q.bounds[2] for q in placed]
    ys = [p[1] for p in P.values()] + [q.bounds[1] for q in placed] + [q.bounds[3] for q in placed]
    x0, x1, y0, y1 = min(xs) - 60, max(xs) + 60, min(ys) - 60, max(ys) + 60
    head = 150
    leg_w, leg_h = 760, 70 + 27 * len(routes)
    W = max(x1 - x0, leg_w + 80)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s %s %s %s" '
           'font-family="Helvetica Neue, Helvetica, Arial, sans-serif">' % (
               f(x0), f(y0 - head), f(W), f(y1 - y0 + head + leg_h + 40))]
    out.append('<rect x="%s" y="%s" width="%s" height="%s" fill="#fff"/>' % (
        f(x0), f(y0 - head), f(W), f(y1 - y0 + head + leg_h + 40)))
    hubname = data.names.get(hub, hub)
    out.append('<text x="%s" y="%s" font-size="56" font-weight="300" fill="#c2185b">%s Routes</text>'
               % (f(x0 + 30), f(y0 - head + 70), esc(hubname)))
    out.append('<text x="%s" y="%s" font-size="19" fill="#444">Normal daytime service pattern: average '
               'trains per hour (tph) each way, Tuesday&#8211;Thursday %02d:00&#8211;%02d:00, September '
               '2026</text>' % (f(x0 + 32), f(y0 - head + 102), WINDOW[0], WINDOW[1]))
    out.append('<text x="%s" y="%s" font-size="14" fill="#777">Source: National Rail Darwin Push '
               'Port, services that ran. Routes show up to %d calling points each side of %s. A '
               'number beside a station is the tph calling there; a white dot means the route '
               'stops.</text>' % (f(x0 + 32), f(y0 - head + 126), K_STOPS, esc(hubname)))
    for rid, d in svg_paths:
        out.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round"/>'
                   % (d, colours[rid], LINE_W))
    hx, hy = P[hub]
    out.append('<rect x="%s" y="%s" width="%s" height="%s" rx="15" fill="#fff" stroke="#111" '
               'stroke-width="3.5"/>' % (f(hx - hub_r), f(hy - hub_r), f(2 * hub_r), f(2 * hub_r)))
    out.append('<text x="%s" y="%s" font-size="22" font-weight="700" fill="#111" text-anchor="middle">'
               '</text>' % (f(hx), f(hy)))
    for _, (x, y) in dots:
        out.append('<circle cx="%s" cy="%s" r="3.9" fill="#fff" stroke="#111" stroke-width="1.5"/>'
                   % (f(x), f(y)))
    # hub label sits above the pill
    out.append('<text x="%s" y="%s" font-size="22" font-weight="700" fill="#111" text-anchor="middle" '
               'stroke="#fff" stroke-width="5" paint-order="stroke">%s <tspan fill="#c2185b" '
               'font-size="15">%s</tspan></text>' % (f(hx), f(hy - hub_r - 12), esc(hubname),
                                                      fmt(sum(r["tph"] for r in routes))))
    for n, mode, tx, ty, anchor, rot, name, t, suffix, tw in label_out:
        tr = ' transform="rotate(%d %s %s)"' % (rot, f(tx), f(ty)) if rot else ""
        sfx = '<tspan dx="6" fill="#888" font-size="12" font-style="italic">%s</tspan>' % esc(suffix.strip()) if suffix else ""
        out.append('<text x="%s" y="%s" font-size="%s" fill="#1a1a1a" text-anchor="%s"%s>'
                   '<tspan font-weight="500">%s</tspan><tspan dx="6" font-size="12" fill="#c2185b" '
                   'font-weight="700">%s</tspan>%s</text>' % (f(tx), f(ty), FONT, anchor, tr, esc(name), t, sfx))
        for j, rid in enumerate(end_label.get(n, [])):
            w = 7.4 * (len(name) + len(t) + 2 + len(suffix)) + 18 + 20 * j
            ca, sa = (1, 0) if rot == 0 else (math.cos(math.radians(rot)), math.sin(math.radians(rot)))
            sign = 1 if anchor == "start" else -1
            bx, by = tx + sign * w * ca, ty + sign * w * sa - 4
            out.append(badge(colours[rid], rid, bx, by, 8.5))
    for end, lst in end_leaf.items():
        ux, uy = lst[0][1]
        m = len(lst)
        for j, (rid, _) in enumerate(lst):
            s = (j - (m - 1) / 2) * 21
            out.append(badge(colours[rid], rid, P[end][0] + ux * 26 - uy * s, P[end][1] + uy * 26 + ux * s, 9.5))
    out.append(legend(routes, colours, data, x0 + 30, y1 + 30))
    out.append("</svg>")
    return "\n".join(out), routes


def label_candidate(mode, x, y, half, w, h):
    if mode == "R":
        tx, ty, anchor, rot = x + half, y + 4.5, "start", 0
        poly = box(tx - 2, ty - h + 3, tx + w, ty + 4)
    elif mode == "L":
        tx, ty, anchor, rot = x - half, y + 4.5, "end", 0
        poly = box(tx - w, ty - h + 3, tx + 2, ty + 4)
    elif mode in ("U", "U2"):
        sx = 0 if mode == "U" else 0
        tx, ty, anchor, rot = x + 2, y - half - (0 if mode == "U" else 0), "start", -45 if mode == "U" else -90
        poly = rot_box(tx, ty, w, h, rot, "start")
    else:
        tx, ty, anchor, rot = x - 2, y + half + 2, "end", -45 if mode == "D" else -90
        poly = rot_box(tx, ty, w, h, rot, "end")
    return mode, poly, tx, ty, anchor, rot


def rot_box(tx, ty, w, h, rot, anchor):
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    x0, x1 = (0, w) if anchor == "start" else (-w, 0)
    pts = [(x0, -h + 3), (x1, -h + 3), (x1, 4), (x0, 4)]
    return Polygon([(tx + px * c - py * s, ty + px * s + py * c) for px, py in pts])


def badge(colour, label, x, y, r):
    return ('<g><circle cx="%s" cy="%s" r="%s" fill="%s"/><text x="%s" y="%s" font-size="%s" '
            'font-weight="700" fill="#fff" text-anchor="middle">%s</text></g>'
            % (f(x), f(y), r, colour, f(x), f(y + r * 0.38), f(r * 1.2), label))


def route_title(r, data):
    fo, fd = r["true_ends"]
    return "%s – %s" % (data.names.get(fo, fo), data.names.get(fd, fd))


def legend(routes, colours, data, x, y):
    rows = ['<text x="%s" y="%s" font-size="22" font-weight="500" fill="#111">Routes</text>' % (f(x), f(y)),
            '<text x="%s" y="%s" font-size="13" fill="#777" text-anchor="end">tph each way</text>'
            % (f(x + 700), f(y))]
    yy = y + 32
    for r in routes:
        c = colours[r["id"]]
        rows.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s"/>'
                    % (f(x + 4), f(yy - 5), f(x + 44), f(yy - 5), c, LINE_W))
        rows.append(badge(c, r["id"], x + 64, yy - 5, 9.5))
        rows.append('<text x="%s" y="%s" font-size="15" fill="#1a1a1a">%s <tspan fill="#888" '
                    'font-size="12">%s</tspan></text>' % (f(x + 84), f(yy), esc(route_title(r, data)), esc(r["toc"])))
        rows.append('<text x="%s" y="%s" font-size="16" font-weight="700" fill="#c2185b" text-anchor="end">'
                    '%.1f</text>' % (f(x + 700), f(yy), r["tph"]))
        yy += 27
    return "\n".join(rows)


def path_d(rid, nodes, off, P):
    d = []
    first = add(P[nodes[0]], off(rid, nodes[0], nodes[1]))
    d.append("M%s %s" % (f(first[0]), f(first[1])))
    for i in range(1, len(nodes) - 1):
        a, v, b = nodes[i - 1], nodes[i], nodes[i + 1]
        o1, o2 = off(rid, a, v), off(rid, v, b)
        t1, t2 = unit(P[a], P[v]), unit(P[v], P[b])
        p1, p2 = add(P[v], o1), add(P[v], o2)
        cross = t1[0] * t2[1] - t1[1] * t2[0]
        if abs(cross) < 1e-6:
            if abs(o1[0] - o2[0]) < 1e-6 and abs(o1[1] - o2[1]) < 1e-6:
                continue
            e = add(p2, t2, TRANSITION)
            c1, c2 = add(p1, t1, TRANSITION * 0.55), add(e, t2, -TRANSITION * 0.55)
            d.append("L%s %s C%s %s %s %s %s %s" % (f(p1[0]), f(p1[1]), f(c1[0]), f(c1[1]),
                                                   f(c2[0]), f(c2[1]), f(e[0]), f(e[1])))
        else:
            c = intersect(p1, t1, p2, t2)
            s, e = add(c, t1, -CORNER_R), add(c, t2, CORNER_R)
            d.append("L%s %s Q%s %s %s %s" % (f(s[0]), f(s[1]), f(c[0]), f(c[1]), f(e[0]), f(e[1])))
    end = add(P[nodes[-1]], off(rid, nodes[-2], nodes[-1]))
    d.append("L%s %s" % (f(end[0]), f(end[1])))
    return " ".join(d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stations", nargs="+", help="CRS codes of hub stations")
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "maps"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    data = Data(a.patterns, a.tiplocs)
    for crs in a.stations:
        res = build_map(data, crs)
        if res is None:
            print(crs, "no routes")
            continue
        svg, routes = res
        open(os.path.join(a.out, crs + ".svg"), "w").write(svg)
        print(crs, data.names.get(crs), len(routes), "routes")


if __name__ == "__main__":
    main()
