"""National route map, one lane per operator, built from the services alone (no hub).

    python src/national_map.py --region london --patterns data/national_patterns.csv \
        --tiplocs data/tiplocs.csv --out maps/london.svg

Graph: stations that a train calls at in succession are joined; a long hop is dropped when shorter
hops already cover it (a fast train passes the stations between). Layout: real positions, spread
outwards from the centre so the dense middle has room. Lanes: one per operator on each edge, in a
fixed operator order so bundles never cross themselves. Dots sit on the operator's own line.
"""
import argparse
import collections
import heapq
import math
import os
import sys
import time

from shapely.geometry import LineString, Point, box
from shapely.strtree import STRtree

import station_route_map as base
from station_route_map import Data, WINDOW, esc, f, fmt_tph
from hub_map import text_w, safe_buffers, DOT_R, DOT_GAP, LABEL_MAX, MAJOR_TPH

PITCH, LINE_W, FONT = 9.5, 5.4, 13.0
MIN_TPH = 0.3
MAX_LANES = 40      # a lane per service pattern; the cap only bites in the busiest London bundles

REGIONS = {  # name: centre (E, N), radius m (None = all of Great Britain), canvas px, spread exponent, min gap px
    "london": {"c": (530000, 180000), "R": 26000, "w": 4600, "h": 4600, "k": 0.55, "gap": 30},
    "uk": {"c": (360000, 600000), "R": None, "w": 11000, "h": 18000, "k": 1.0, "gap": 40},
}

OPERATORS = {  # code: (name, colour)
    "SR": ("ScotRail", "#1d3f8f"), "NT": ("Northern", "#6a2c91"), "GW": ("Great Western Railway", "#0a5c36"),
    "SE": ("Southeastern", "#1fa0e0"), "SN": ("Southern", "#7ab800"), "SW": ("South Western Railway", "#d4352f"),
    "LO": ("London Overground", "#f28c00"), "LE": ("Greater Anglia", "#d6005d"), "LM": ("West Midlands / London Northwestern", "#00a56a"),
    "AW": ("Transport for Wales", "#e5362b"), "TL": ("Thameslink", "#c2185b"), "LT": ("Thameslink / Gatwick", "#e91e8c"),
    "XR": ("Elizabeth line", "#6950a1"), "ME": ("Merseyrail", "#f4c20d"), "EM": ("East Midlands Railway", "#8b4a1c"),
    "TP": ("TransPennine Express", "#00a9e0"), "VT": ("Avanti West Coast", "#0b3a5a"), "CC": ("c2c", "#b5006e"),
    "XC": ("CrossCountry", "#a4343a"), "GR": ("LNER", "#d3202f"), "GN": ("Great Northern", "#4b2e83"),
    "CH": ("Chiltern", "#00b2a9"), "TW": ("Tyne & Wear Metro", "#f0b400"), "HX": ("Heathrow Express", "#505050"),
    "SJ": ("Sheffield Supertram", "#8dc63f"), "IL": ("Island Line", "#2e8b57"), "GX": ("Gatwick Express", "#ee3124"),
    "ES": ("Eurostar", "#ffd200"), "GC": ("Grand Central", "#111111"), "HT": ("Hull Trains", "#9d1c7b"),
    "LD": ("Lumo", "#3dbeb4"), "CS": ("Caledonian Sleeper", "#2a2a72"), "LF": ("Lumo/Other", "#888888"),
    "NY": ("North Yorkshire Moors", "#777777"),
}
ORDER = list(OPERATORS)


# TIPLOCs with no row in tiplocs.csv: same station under another code, or a new station
SAME_AS = {"SPL": "STP", "LIVSTLL": "LST", "PADTLL": "PAD", "ABWDXR": "ABW", "WOLWXR": "WWA", "WCHAPXR": "ZWL",
           "FRNDXR": "ZFD", "CNNBELL": "CNN", "HIGHBYE": "HHY", "STPANCI": "STP", "EBSFLTI": "EBD",
           "STFORDI": "SFA", "BARKRIV": "BKG"}
NEW_NAMES = {"CANWHRF": "Canary Wharf", "CUSTMHS": "Custom House", "TOTCTRD": "Tottenham Court Road",
             "BONDST": "Bond Street", "BRENTX": "Brent Cross West", "CAMBSTH": "Cambridge South",
             "EDINAIR": "Edinburgh Airport", "EPHT": "Heathrow Express"}


def repair(data):
    """Fold alias codes into their station, name the new ones, and place stations with no
    coordinates halfway between the stations that adjoin them in the services."""
    seen = set()
    fixed = []
    for toc, stops, times in data.services:
        out, ot = [], []
        for c, t in zip(stops, times):
            c = SAME_AS.get(c, c)
            if not out or out[-1] != c:
                out.append(c)
                ot.append(t)
        fixed.append((toc, tuple(out), tuple(ot)))
    data.services = fixed
    data.real = {c for c in data.xy} | set(NEW_NAMES)
    data.names.update(NEW_NAMES)
    nb = collections.defaultdict(set)
    for _, stops, _ in fixed:
        for a, b in zip(stops, stops[1:]):
            nb[a].add(b)
            nb[b].add(a)
    missing = [c for c in nb if c not in data.xy]
    for _ in range(40):
        for c in missing:
            pts = [data.xy[x] for x in nb[c] if x in data.xy]
            if pts:
                data.xy[c] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    return len(missing)


def colour(rid):
    return OPERATORS.get(rid.split("#")[0], (rid, "#777777"))[1]


def okey(rid):
    toc, n = rid.split("#")
    return (ORDER.index(toc) if toc in ORDER else 99, int(n))


def label_routes(data, keep):
    """Replace each service's operator by a route id 'TOC#n': services of one operator whose
    stopping patterns (inside the region) mostly overlap are one route; nothing else is merged."""
    counts = collections.Counter()
    clipped = []
    for toc, stops, times in data.services:
        seq = tuple(c for c in stops if c in keep)
        if len(seq) < 2:
            clipped.append(None)
            continue
        key = (toc, seq if seq <= seq[::-1] else seq[::-1])
        counts[key] += 1
        clipped.append(key)
    clusters, by_station, rid_of = [], collections.defaultdict(list), {}
    per_toc = collections.Counter()
    for key, n in counts.most_common():
        toc, seq = key
        st = set(seq)
        best, bj = None, 0.0
        cand = {i for c in seq for i in by_station[(toc, c)]}
        for i in cand:
            cs = clusters[i]
            inter = len(st & cs)
            if inter / len(st) < 0.8:
                continue
            j = inter / len(st | cs)
            if j > bj:
                best, bj = i, j
        if best is not None and bj >= 0.5:
            rid_of[key] = clusters[best][1] if False else best
            clusters[best] |= st
        else:
            clusters.append(set(st))
            best = len(clusters) - 1
            for c in seq:
                by_station[(toc, c)].append(best)
            rid_of[key] = best
        # clusters grow, so index every new member station too
        for c in st:
            if best not in by_station[(toc, c)]:
                by_station[(toc, c)].append(best)
    names = {}
    for key, i in rid_of.items():
        if (key[0], i) not in names:
            per_toc[key[0]] += 1
            names[(key[0], i)] = "%s#%d" % (key[0], per_toc[key[0]])
    data.services = [(names[(k[0], rid_of[k])] if k else toc, stops, times)
                     for k, (toc, stops, times) in zip(clipped, data.services)]
    return len(names)


# ------------------------------------------------------------------ graph
def in_window(t):
    return WINDOW[0] <= int(t[:2]) < WINDOW[1]


def build_graph(data, keep):
    """-> edges {(u,v) u<v: Counter(toc)} (service counts), calls {crs: Counter(toc)}, stats."""
    xy = data.xy
    dist = lambda p, q: math.hypot(xy[p][0] - xy[q][0], xy[p][1] - xy[q][1])
    hops = collections.defaultdict(collections.Counter)     # (a,b) sorted -> toc -> n
    calls = collections.defaultdict(collections.Counter)
    runs = []                                               # (toc, [stations]) clipped to the region
    for toc, stops, times in data.services:
        run = []
        for c, t in zip(stops, times):
            if c in keep:
                if in_window(t):
                    calls[c][toc] += 1
                run.append((c, t))
            else:
                if len(run) > 1:
                    runs.append((toc, run))
                run = []
        if len(run) > 1:
            runs.append((toc, run))
    for toc, run in runs:
        for (a, ta), (b, _) in zip(run, run[1:]):
            if a != b and in_window(ta):
                hops[(a, b) if a < b else (b, a)][toc] += 1
    adj = collections.defaultdict(set)
    for a, b in hops:
        adj[a].add(b)
        adj[b].add(a)
    # longest first: drop a hop when the other hops already join its ends without a big detour
    graph = collections.defaultdict(dict)
    for a, b in hops:
        graph[a][b] = graph[b][a] = dist(a, b)
    for a, b in sorted(hops, key=lambda h: -dist(*h)):
        limit = 1.35 * graph[a][b]
        del graph[a][b], graph[b][a]
        if not detour_within(graph, a, b, limit):
            graph[a][b] = graph[b][a] = dist(a, b)
    return runs, calls, graph, hops


def detour_within(graph, a, b, limit):
    d, pq = {a: 0.0}, [(0.0, a)]
    while pq:
        w, u = heapq.heappop(pq)
        if u == b:
            return True
        if w > d[u]:
            continue
        for v, l in graph[u].items():
            nw = w + l
            if nw <= limit and nw < d.get(v, 1e18):
                d[v] = nw
                heapq.heappush(pq, (nw, v))
    return False


def route(graph, a, b, cache):
    k = (a, b)
    if k in cache:
        return cache[k]
    d, prev, pq = {a: 0.0}, {}, [(0.0, a)]
    while pq:
        w, u = heapq.heappop(pq)
        if u == b:
            break
        if w > d[u]:
            continue
        for v, l in graph[u].items():
            nw = w + l
            if nw < d.get(v, 1e18):
                d[v], prev[v] = nw, u
                heapq.heappush(pq, (nw, v))
    if b not in d:
        cache[k] = None
        return None
    out = [b]
    while out[-1] != a:
        out.append(prev[out[-1]])
    cache[k] = out[::-1]
    return cache[k]


def edge_loads(data, runs, graph):
    """Services per (edge, route), and for each lane end the stations the route goes on to."""
    slots = data.days * (WINDOW[1] - WINDOW[0])
    cache, load = {}, collections.defaultdict(collections.Counter)
    beyond = collections.defaultdict(collections.Counter)   # (edge, rid, end node) -> neighbour -> n
    for toc, run in runs:
        full = []
        for (a, ta), (b, _) in zip(run, run[1:]):
            if a == b:
                continue
            if b in graph[a]:
                p = [a, b]
            else:
                p = route(graph, a, b, cache)
                if p is None:
                    graph[a][b] = graph[b][a] = 1.0   # no way round: keep the direct edge
                    p = [a, b]
            if in_window(ta):
                for u, v in zip(p, p[1:]):
                    load[(u, v) if u < v else (v, u)][toc] += 1
            full.extend(p if not full else p[1:])
        for x, y, z in zip(full, full[1:], full[2:]):
            beyond[((x, y) if x < y else (y, x), toc, y)][z] += 1
            beyond[((y, z) if y < z else (z, y), toc, y)][x] += 1
    scale = 1.0 / slots / 2
    return {e: {t: n * scale for t, n in c.items()} for e, c in load.items()}, scale, beyond


# ------------------------------------------------------------------ geometry
def project(data, keep, region):
    import numpy as np
    r = REGIONS[region]
    cx, cy = r["c"]
    cs = sorted(keep)
    P = np.zeros((len(cs), 2))
    for i, c in enumerate(cs):
        e, n = data.xy[c][0] - cx, -data.xy[c][1] - cy
        if r["R"]:
            d = math.hypot(e, n)
            dd = r["w"] / 2 * (min(d, r["R"] * 1.2) / r["R"]) ** r["k"] if d else 0.0
            P[i] = (r["w"] / 2 + (e / d * dd if d else 0), r["h"] / 2 - (n / d * dd if d else 0))
        else:
            sc = r["h"] / 1250000.0
            P[i] = (r["w"] / 2 + e * sc, r["h"] / 2 - n * sc)
    P0, D = P.copy(), r["gap"]
    for it in range(120):   # push stations closer than the gap apart, but stay near the true position
        diff = P[:, None, :] - P[None, :, :]
        d = np.hypot(diff[..., 0], diff[..., 1]) + np.eye(len(cs)) * 1e9
        close = d < D
        if not close.any():
            break
        push = (np.where(close, (D - d) / d / 2, 0.0)[..., None] * diff).sum(1)
        P += np.clip(push, -6, 6) + 0.01 * (P0 - P)
    return {c: (float(P[i, 0]), float(P[i, 1])) for i, c in enumerate(cs)}


def unit(p, q):
    L = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
    return ((q[0] - p[0]) / L, (q[1] - p[1]) / L), L


def lane_side(pos, beyond, e, t):
    """Where a lane belongs across its edge: routes that go on to the right of the edge (seen
    travelling u -> v) sit on the right at both ends, so bundles only reorder where they diverge."""
    u, v = e
    (dx, dy), _ = unit(pos[u], pos[v])
    tot = 0.0
    for end in (u, v):
        c = beyond.get((e, t, end))
        if not c:
            continue
        n_ = sum(c.values())
        for z, k in c.items():
            (wx, wy), L = unit(pos[end], pos[z])
            tot += k / n_ * (dx * wy - dy * wx) * (1 if end == v else -1)
    return tot


def build(data, region, verbose=True):
    r = REGIONS[region]
    cx, cy = r["c"]
    keep = {c for c, (e, y) in data.xy.items()
            if c in data.real and (not r["R"] or math.hypot(e - cx, -y - cy) <= r["R"])}
    m_routes = label_routes(data, keep)
    runs, calls, graph, hops = build_graph(data, keep)
    load, scale, beyond = edge_loads(data, runs, graph)
    pos = project(data, keep, region)

    # lanes per edge
    lanes = {}
    merged = 0
    for e, c in load.items():
        ts = [t for t, v in c.items() if v >= MIN_TPH]
        if len(ts) > MAX_LANES:
            merged += len(ts) - MAX_LANES
            ts = sorted(ts, key=lambda t: -c[t])[:MAX_LANES]
        ts.sort(key=okey)
        ts.sort(key=lambda t: lane_side(pos, beyond, e, t))
        if ts:
            lanes[e] = ts
    # stations shown: some kept lane through the node stops there
    call_tph = {c: {t: n * scale for t, n in tc.items()} for c, tc in calls.items()}
    shown_lanes = collections.defaultdict(set)      # node -> tocs with a lane through it
    for (u, v), ts in lanes.items():
        for t in ts:
            shown_lanes[u].add(t)
            shown_lanes[v].add(t)

    # per node, per operator: lane end points on each incident edge
    ends = collections.defaultdict(lambda: collections.defaultdict(list))
    segs = []   # (toc, p, q, edge)
    for (u, v), ts in lanes.items():
        d, L = unit(pos[u], pos[v])
        n = (-d[1], d[0])
        m = len(ts)
        for i, t in enumerate(ts):
            off = (i - (m - 1) / 2) * PITCH
            pu = (pos[u][0] + n[0] * off, pos[u][1] + n[1] * off)
            pv = (pos[v][0] + n[0] * off, pos[v][1] + n[1] * off)
            ends[u][t].append(pu)
            ends[v][t].append(pv)
            segs.append((t, pu, pv, (u, v)))
    dots = {}   # (node, toc) -> point
    fans = []   # (toc, p, q)
    for node, per in ends.items():
        for t, pts in per.items():
            q = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            for p in pts:
                fans.append((t, p, q))
            if call_tph.get(node, {}).get(t, 0) >= MIN_TPH:
                dots[(node, t)] = q
    return {"pos": pos, "lanes": lanes, "segs": segs, "fans": fans, "dots": dots, "call_tph": call_tph,
            "load": load, "merged": merged, "routes": m_routes, "w": r["w"], "h": r["h"]}


# ------------------------------------------------------------------ render
def render(m, data, title):
    xs_ = [q[0] for _, p, q, _ in m["segs"]] + [p[0] for _, p, q, _ in m["segs"]]
    ys_ = [q[1] for _, p, q, _ in m["segs"]] + [p[1] for _, p, q, _ in m["segs"]]
    X0, Y0 = min(xs_) - 400, min(ys_) - 150
    W, H = max(xs_) + 400 - X0, max(ys_) + 150 - Y0
    pos, dots, call_tph = m["pos"], m["dots"], m["call_tph"]
    # stations with dots
    nodes = collections.defaultdict(list)
    for (n, t), q in dots.items():
        nodes[n].append((t, q))
    # nudge dots of one station apart if they sit closer than DOT_GAP
    for n, ds in nodes.items():
        for _ in range(20):
            moved = False
            for i in range(len(ds)):
                for j in range(i + 1, len(ds)):
                    (ti, pi), (tj, pj) = ds[i], ds[j]
                    dx, dy = pj[0] - pi[0], pj[1] - pi[1]
                    d = math.hypot(dx, dy)
                    if d < DOT_GAP:
                        if d < 1e-6:
                            dx, dy, d = 1.0, 0.0, 1.0
                        s = (DOT_GAP - d) / 2 + 0.1
                        ds[i] = (ti, (pi[0] - dx / d * s, pi[1] - dy / d * s))
                        ds[j] = (tj, (pj[0] + dx / d * s, pj[1] + dy / d * s))
                        moved = True
            if not moved:
                break
    # obstacles
    obst = [LineString([p, q]).buffer(LINE_W / 2) for _, p, q, _ in m["segs"]]
    obst += [LineString([p, q]).buffer(LINE_W / 2) for _, p, q in m["fans"] if p != q]
    dot_geo = [Point(q).buffer(DOT_R + 1.5) for n, ds in nodes.items() for _, q in ds]
    # bundle tph on long edges
    elabels = []
    for (u, v), ts in m["lanes"].items():
        (x0, y0), (x1, y1) = pos[u], pos[v]
        if math.hypot(x1 - x0, y1 - y0) >= 170:
            tot = sum(m["load"][(u, v)][t] for t in ts)
            txt = fmt_tph(tot)
            w_ = text_w(txt, FONT - 3)
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            elabels.append((mx, my, txt, box(mx - w_ / 2 - 2, my - 7, mx + w_ / 2 + 2, my + 7)))
    static = obst + dot_geo + [b for *_, b in elabels]
    tree = STRtree(static)
    placed, placed_tree_boxes = [], []
    labels, leaders, fails = {}, 0, 0
    order = sorted(nodes, key=lambda n: -sum(call_tph[n].values()))
    for n in order:
        ds = nodes[n]
        cx = sum(q[0] for _, q in ds) / len(ds)
        cy = sum(q[1] for _, q in ds) / len(ds)
        rho = max(math.hypot(q[0] - cx, q[1] - cy) for _, q in ds) + DOT_R
        name = data.names.get(n, n)
        tph = sum(call_tph[n].values())
        txt = name
        nw_, tw_ = text_w(txt, FONT), text_w(fmt_tph(tph), FONT - 3)
        w, h = nw_ + tw_ + 2, FONT + 2
        best = None
        for extra in (4, 10, 18, 28, 40, 60, 90):
            r = rho + extra
            cands = [("start", cx + r, cy), ("end", cx - r, cy), ("start", cx + r * 0.7, cy - r * 0.7 - h / 2),
                     ("end", cx - r * 0.7, cy - r * 0.7 - h / 2), ("start", cx + r * 0.7, cy + r * 0.7 + h / 2),
                     ("end", cx - r * 0.7, cy + r * 0.7 + h / 2), ("middle", cx, cy - r - h / 2),
                     ("middle", cx, cy + r + h / 2)]
            for anchor, x, y in cands:
                x0 = x if anchor == "start" else x - w if anchor == "end" else x - w / 2
                bx = box(x0, y - h / 2, x0 + w, y + h / 2)
                if bx.bounds[0] < X0 or bx.bounds[2] > X0 + W or bx.bounds[1] < Y0 or bx.bounds[3] > Y0 + H:
                    continue
                if len(tree.query(bx, predicate="intersects")):
                    continue
                if any(bx.intersects(b) for b in placed):
                    continue
                best = (anchor, x, y, bx, extra)
                break
            if best:
                break
        if best is None:
            fails += 1
            continue
        anchor, x, y, bx, extra = best
        placed.append(bx)
        labels[n] = (bx.bounds[0], y, nw_ + 4, txt, tph, extra)
        if extra > LABEL_MAX:
            leaders += 1

    # crossings between lanes of different edges (not at a shared station)
    short = []
    for t, p, q, e in m["segs"]:
        (dx, dy), L = unit(p, q)
        if L > 12:
            short.append((e, LineString([(p[0] + dx * 4, p[1] + dy * 4), (q[0] - dx * 4, q[1] - dy * 4)])))
    st = STRtree([g for _, g in short])
    crossings = 0
    for i, (e, g) in enumerate(short):
        for j in st.query(g, predicate="intersects"):
            e2 = short[j][0]
            if j > i and e2 != e:
                shared = set(e) & set(e2)
                if shared:   # lanes of edges meeting at a station fan in and out near it: not a crossing
                    pt = g.intersection(short[j][1]).centroid
                    n_ = shared.pop()
                    if math.hypot(pt.x - pos[n_][0], pt.y - pos[n_][1]) < 4 * PITCH * 3:
                        continue
                crossings += 1
    # report
    flat = [(n, t, q) for n, ds in nodes.items() for t, q in ds]
    dot_overlap = 0
    pts = STRtree([Point(q) for _, _, q in flat])
    for i, (_, _, q) in enumerate(flat):
        for j in pts.query(Point(q).buffer(DOT_GAP)):
            if j > i and math.hypot(flat[j][2][0] - q[0], flat[j][2][1] - q[1]) < DOT_GAP - 1e-6:
                dot_overlap += 1
    report = {"stations": len(nodes), "labelled": len(labels), "unlabelled": fails, "dot_overlap": dot_overlap,
              "crossings": crossings, "leader_lines": leaders, "lanes_dropped_by_cap": m["merged"], "routes": m["routes"]}

    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%d %d %d %d" font-family="Helvetica,Arial,sans-serif">' % (X0, Y0, W, H),
           '<rect x="%d" y="%d" width="%d" height="%d" fill="#fff"/>' % (X0, Y0, W, H),
           '<text x="%d" y="%d" font-size="34" font-weight="700">%s</text>' % (X0 + 30, Y0 + 50, esc(title))]
    for t, p, q, _ in m["segs"]:
        out.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s" stroke-linecap="round"/>'
                   % (f(p[0]), f(p[1]), f(q[0]), f(q[1]), colour(t), LINE_W))
    for t, p, q in m["fans"]:
        if p != q:
            out.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s" stroke-linecap="round"/>'
                       % (f(p[0]), f(p[1]), f(q[0]), f(q[1]), colour(t), LINE_W))
    # capsules at major stations
    for n, ds in nodes.items():
        big = [t for t in call_tph[n] if call_tph[n][t] >= MIN_TPH]
        if sum(call_tph[n].values()) >= MAJOR_TPH and len(big) >= 2 and len(ds) >= 2:
            xs, ys = [q[0] for _, q in ds], [q[1] for _, q in ds]
            out.append('<rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="#fff" fill-opacity="0.0" stroke="#222" '
                       'stroke-width="2.2"/>' % (f(min(xs) - DOT_R - 3), f(min(ys) - DOT_R - 3),
                                                 f(max(xs) - min(xs) + 2 * DOT_R + 6), f(max(ys) - min(ys) + 2 * DOT_R + 6),
                                                 f(DOT_R + 3)))
    for n, ds in nodes.items():
        for t, q in ds:
            out.append('<circle cx="%s" cy="%s" r="%s" fill="#fff" stroke="%s" stroke-width="2.2"/>'
                       % (f(q[0]), f(q[1]), DOT_R, colour(t)))
    for n, (x0, y, off, txt, tph, extra) in labels.items():
        out.append('<text x="%s" y="%s" font-size="%s" dominant-baseline="central" fill="#111">%s</text>'
                   '<text x="%s" y="%s" font-size="%s" dominant-baseline="central" fill="#666">%s</text>'
                   % (f(x0), f(y), FONT, esc(txt), f(x0 + off), f(y), FONT - 3, fmt_tph(tph)))
    for mx, my, txt, _ in elabels:
        out.append('<text x="%s" y="%s" font-size="%s" text-anchor="middle" dominant-baseline="central" fill="#444" '
                   'stroke="#fff" stroke-width="3" paint-order="stroke">%s</text>' % (f(mx), f(my), FONT - 3, txt))
    # legend
    used = sorted({t.split("#")[0] for ts in m["lanes"].values() for t in ts}, key=lambda t: ORDER.index(t) if t in ORDER else 99)
    for i, t in enumerate(used):
        y = Y0 + 90 + i * 24
        out.append('<rect x="%d" y="%d" width="36" height="8" rx="4" fill="%s"/><text x="%d" y="%d" font-size="15">%s</text>'
                   % (X0 + 30, y, colour(t), X0 + 76, y + 8, esc(OPERATORS.get(t, (t,))[0])))
    out.append("</svg>")
    return "\n".join(out), report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="london", choices=list(REGIONS))
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    t0 = time.time()
    data = Data(a.patterns, a.tiplocs)
    repair(data)
    m = build(data, a.region)
    svg, report = render(m, data, "%s (typical weekday 09:00-16:00)" % a.region.title())
    out = a.out or os.path.join("maps", a.region + ".svg")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w").write(svg)
    print(report, "%.0fs" % (time.time() - t0), out)


if __name__ == "__main__":
    sys.exit(main())
