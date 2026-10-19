"""Route map centred on a station, drawn to explicit rules.

    poetry run python3 hub_map.py BRI DON RDG --patterns national.csv --tiplocs tiplocs_geo.csv

Rules (each is measured; the report is printed per map and drives the spacing search):
  dots      one dot per route per station, never closer than DOT_GAP
  junctions only a station where two routes that both stop there go different ways is marked
            as an interchange; stations merely served by several routes are not
  lanes     parallel lanes, ordered by where each route goes next, so lines only change
            side where they diverge; no line runs under another
  crossings none, except inside the hub / interchange marks
  routes    drawn in full to their termini; stations no shown route stops at are omitted;
            at most MAX_ROUTES routes, near-identical patterns merged
The PDF route map (data/nr_schematic.json) is used only for topology: which stations a fast
service passes between two stops.
"""
import argparse
import collections
import math
import os

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points

import station_route_map as base
from station_route_map import (PALETTE, FONT, LINE_W, WINDOW, Data, add, badge, discover_routes, esc, f,
                               fmt_tph, label_candidate, layout, legend, route_tree_nodes, unit)
import nr_route_map as nrm

HERE = os.path.dirname(os.path.abspath(__file__))
SPACINGS = [250, 180, 130, 100, 80, 64, 52, 47]
MAX_ROUTES = 12
LABEL_MAX = 30.0  # a label may start at most this far from its own station's dots
MAJOR_TPH = 3.0   # an interchange capsule needs at least this many tph calling
DOT_R = 3.9
DOT_GAP = 8.4
base.PITCH = 9.5
PITCH = base.PITCH


# ------------------------------------------------------------------ topology from the PDF
def load_network(path, data):
    """The PDF-derived network, or None (the map is then built from the services alone)."""
    path = path or os.path.join(HERE, "..", "data", "nr_schematic.json")
    if not os.path.exists(path):
        print("note: no network file at %s; fast services will not pick up stations they pass" % path)
        return None
    nr = nrm.Network(path)
    nr.build_anchors(data)
    return nr


def expand_via_pdf(routes, nr, data):
    """Insert, between two consecutive stops, the shown stations the service passes (per the PDF)."""
    if nr is None:
        for r in routes:
            r["path"] = list(r["seq"])
        close_triangles(routes, data)
        return
    universe = {c for r in routes for c in r["seq"]}
    node_of, crs_of = {}, {}
    for c in universe:
        n = nr.match(c, data.names.get(c, c), data)
        if n is not None and n not in crs_of:
            node_of[c], crs_of[n] = n, c
    for r in routes:
        seq = r["seq"]
        out = [seq[0]]
        for c in seq[1:]:
            a = out[-1]
            if a in node_of and c in node_of:
                p = nr.path(node_of[a], node_of[c])
                if p and len(p) < 70:
                    for n in p[1:-1]:
                        cc = crs_of.get(n)
                        if cc and cc not in out and cc != c and cc != seq[-1]:
                            out.append(cc)
            out.append(c)
        r["path"] = out
    close_triangles(routes, data)


def close_triangles(routes, data):
    """If routes use a-b and b-c, a route going straight a->c passes b when b lies roughly between
    them (the PDF sometimes draws a bypass line past a station the train really goes through)."""
    xy = data.xy

    def dist(p, q):
        return math.hypot(xy[p][0] - xy[q][0], xy[p][1] - xy[q][1])

    def between(a, b, c):
        ax, ay, cx, cy = xy[a][0], xy[a][1], xy[c][0], xy[c][1]
        L2 = (cx - ax) ** 2 + (cy - ay) ** 2 or 1.0
        t = ((xy[b][0] - ax) * (cx - ax) + (xy[b][1] - ay) * (cy - ay)) / L2
        return -0.05 < t < 1.05

    for _ in range(4):
        edges = collections.defaultdict(set)
        for r in routes:
            for a, b in zip(r["path"], r["path"][1:]):
                edges[a].add(b)
                edges[b].add(a)
        changed = False
        for r in routes:
            out = [r["path"][0]]
            for c in r["path"][1:]:
                a = out[-1]
                if a in xy and c in xy:
                    for b in edges[a] & edges[c]:
                        if b in xy and b not in out and b != c and b != r["path"][-1] \
                                and dist(a, b) + dist(b, c) <= 2.0 * dist(a, c) and between(a, b, c):
                            out.append(b)
                            changed = True
                            break
                out.append(c)
            r["path"] = out
        if not changed:
            break


# ------------------------------------------------------------------ geometry helpers
def cubic(p0, p1, p2, p3, t):
    a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def quad(p0, p1, p2, t):
    a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
    return (a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1])


def path_geom(rid, nodes, off, P):
    """SVG path data plus a polyline sample of it (for crossing checks)."""
    d, pts = [], []
    first = add(P[nodes[0]], off(rid, nodes[0], nodes[1]))
    d.append("M%s %s" % (f(first[0]), f(first[1])))
    pts.append(first)
    for i in range(1, len(nodes) - 1):
        a, v, b = nodes[i - 1], nodes[i], nodes[i + 1]
        o1, o2 = off(rid, a, v), off(rid, v, b)
        t1, t2 = unit(P[a], P[v]), unit(P[v], P[b])
        p1, p2 = add(P[v], o1), add(P[v], o2)
        cross = t1[0] * t2[1] - t1[1] * t2[0]
        if abs(cross) < 1e-6 and t1[0] * t2[0] + t1[1] * t2[1] < 0:
            # the route turns round here (out to a spur's end station and back): go all the way out
            d.append("L%s %s" % (f(p1[0]), f(p1[1])))
            pts.append(p1)
            continue
        if abs(cross) < 1e-6:
            if abs(o1[0] - o2[0]) < 1e-6 and abs(o1[1] - o2[1]) < 1e-6:
                continue
            T = base.TRANSITION
            e = add(p2, t2, T)
            c1, c2 = add(p1, t1, T * 0.55), add(e, t2, -T * 0.55)
            d.append("L%s %s C%s %s %s %s %s %s" % (f(p1[0]), f(p1[1]), f(c1[0]), f(c1[1]), f(c2[0]), f(c2[1]),
                                                   f(e[0]), f(e[1])))
            pts += [cubic(p1, c1, c2, e, k / 8) for k in range(9)]
        else:
            c = base.intersect(p1, t1, p2, t2)
            s, e = add(c, t1, -base.CORNER_R), add(c, t2, base.CORNER_R)
            d.append("L%s %s Q%s %s %s %s" % (f(s[0]), f(s[1]), f(c[0]), f(c[1]), f(e[0]), f(e[1])))
            pts += [quad(s, c, e, k / 6) for k in range(7)]
    end = add(P[nodes[-1]], off(rid, nodes[-2], nodes[-1]))
    d.append("L%s %s" % (f(end[0]), f(end[1])))
    pts.append(end)
    return " ".join(d), pts


def safe_buffers(pts, radius, chunk=10):
    """Buffer a polyline in short pieces, splitting where it doubles back. GEOS can crash (not just
    raise) on long lines that retrace themselves, which would take a whole worker process down."""
    pieces, cur = [], [pts[0]]
    for i in range(1, len(pts)):
        cur.append(pts[i])
        reverses = False
        if i + 1 < len(pts):
            a = (pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
            b = (pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
            na, nb = math.hypot(*a), math.hypot(*b)
            reverses = na > 1e-9 and nb > 1e-9 and (a[0] * b[0] + a[1] * b[1]) / (na * nb) < -0.9
        if reverses or len(cur) >= chunk:
            pieces.append(cur)
            cur = [pts[i]]
    if len(cur) >= 2:
        pieces.append(cur)
    return [LineString(c).buffer(radius) for c in pieces if len(c) >= 2]


def render(data, hub, routes, spacing):
    base.SPACING = float(spacing)
    L = layout(routes, hub, data)
    parent, children, P = L["parent"], L["children"], L["P"]
    hub_r = L["hub_r"]
    for r in routes:
        tn = []
        for n in route_tree_nodes(r["path"], parent):
            # an out-and-back detour is dropped only if the route does not stop at the station it visits
            if len(tn) >= 2 and tn[-2] == n and tn[-1] != hub and tn[-1] not in r["calls"]:
                tn.pop()
            else:
                tn.append(n)
        dn = [tn[0]]
        for a, b in zip(tn, tn[1:]):
            seg = L["edge_path"][(a, b)] if parent.get(b) == a else L["edge_path"][(b, a)][::-1]
            dn += seg[1:]
        r["draw"], r["tree_nodes"] = dn, tn

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

    canon, tree_of_draw = {}, {}
    for (p, c), seq in L["edge_path"].items():
        for a, b in zip(seq, seq[1:]):
            canon[(a, b)] = canon[(b, a)] = (a, b)
            tree_of_draw[(a, b)] = (a, c) if b == "j:" + a else (None, c)
    lanes = {}
    for ab, (shared, c) in tree_of_draw.items():
        members = (subtree[shared] - {shared}) if shared else subtree[c]
        users = []
        for r in routes:
            dr = r["draw"]
            if any({dr[i], dr[i + 1]} == set(ab) for i in range(len(dr) - 1)):
                inside = [n for n in r["tree_nodes"] if n in members]
                if inside:
                    z = max(inside, key=lambda n: depth[n])
                    users.append((pre[z], r["id"]))  # rule: lane order follows where each route goes next
        lanes[ab] = [rid for _, rid in sorted(users)]

    # lanes keep their absolute offset along a bundle (a route leaving just peels away; nothing
    # re-centres), so lines do not wiggle after a junction. Only the hub's edges are centred.
    end_edge = {ab[1]: ab for ab in lanes}
    offs = {}

    def solve(ab):
        if ab in offs:
            return
        order = lanes[ab]
        n = len(order)
        par = end_edge.get(ab[0]) if ab[0] != hub else None
        if par is not None and par in lanes:
            solve(par)
            cur = {rid: offs[par][rid] for rid in order if rid in offs[par]}
            if len(cur) == n:
                offs[ab] = cur
                return
        offs[ab] = {rid: ((n - 1) / 2 - i) * PITCH for i, rid in enumerate(order)}

    for ab in lanes:
        solve(ab)
    ext = {ab: (min(o.values()), max(o.values())) for ab, o in offs.items() if o}

    def off(rid, a, b):
        ab = canon[(a, b)]
        s = offs[ab].get(rid)
        if s is None:  # route backtracks over this edge: park it outside the bundle
            s = ext[ab][1] + PITCH
        d = unit(P[ab[0]], P[ab[1]])
        return (d[1] * s, -d[0] * s)

    colours = {r["id"]: PALETTE[i % len(PALETTE)] for i, r in enumerate(routes)}
    paths, dots = [], []
    end_leaf, end_label = collections.defaultdict(list), collections.defaultdict(list)
    for r in routes:
        dn = r["draw"]
        k = dn.index(hub)
        for part in (dn[: k + 1], dn[k:]):
            if len(part) >= 2:
                d, pts = path_geom(r["id"], part, off, P)
                paths.append((r["id"], d, pts))
        for end, nxt in ((dn[0], dn[1]), (dn[-1], dn[-2])):
            if end == hub:
                continue
            if not children.get(end):
                end_leaf[end].append((r["id"], unit(P[nxt], P[end])))
            else:
                end_label[end].append(r["id"])
        for i, n in enumerate(dn):
            if n in r["calls"] and n != hub and not n.startswith("j:"):
                a, b = (dn[i - 1], n) if i else (n, dn[1])
                if not any(d[0] == r["id"] and d[1] == n for d in dots):
                    dots.append((r["id"], n, add(P[n], off(r["id"], a, b))))

    tph = collections.defaultdict(float)
    for r in routes:
        for c in r["calls"]:
            tph[c] += r["tph"]

    # interchange rule: two routes that both stop here and then go different ways
    ways = collections.defaultdict(set)
    for r in routes:
        path = r["path"]
        for c in r["calls"]:
            if c in path and c != hub:
                i = path.index(c)
                ways[c].add(frozenset(x for x in (path[i - 1] if i else None,
                                                  path[i + 1] if i + 1 < len(path) else None) if x))
    interchange = {c for c, w in ways.items() if len(w) >= 2 and tph[c] >= MAJOR_TPH}

    bundle = collections.defaultdict(float)
    obstacles, edge_geoms = [], []
    for ab, order in lanes.items():
        mn, mx = ext.get(ab, (0.0, 0.0))
        half_w = max(abs(mn), abs(mx)) + PITCH / 2
        w = half_w + 3
        edge_geoms.append((ab, LineString([P[ab[0]], P[ab[1]]]), w))
        for n in ab:
            bundle[n] = max(bundle[n], half_w)
    pill = Point(P[hub]).buffer(hub_r * 1.45)
    obstacles.append(pill)
    for _rid, _d, _pts in paths:  # labels must clear the real lines, not the whole bundle width
        obstacles.extend(safe_buffers(_pts, LINE_W / 2 + 2.3))
    caps = {}
    for n in interchange:
        th = L["dir"].get(n, 0.0)
        c, s_ = math.cos(th), math.sin(th)
        ab = end_edge.get(n)
        mn, mx = ext.get(ab, (0.0, 0.0)) if ab else (0.0, 0.0)
        mid = (mn + mx) / 2
        cx_, cy_ = P[n][0] + s_ * mid, P[n][1] - c * mid
        hw = (mx - mn) / 2 + PITCH / 2 + 5
        caps[n] = (cx_, cy_, math.degrees(th), hw)
        obstacles.append(Polygon([(cx_ + x * c - y * s_, cy_ + x * s_ + y * c)
                                  for x, y in [(-8, -hw), (8, -hw), (8, hw), (-8, hw)]]).buffer(3))
    by_stn = collections.defaultdict(list)
    for i, (rid, n, pt) in enumerate(dots):
        by_stn[n].append(i)
    for n in caps:
        idx = sorted(by_stn[n], key=lambda i: (dots[i][2][0] * -math.sin(math.radians(caps[n][2]))
                                               + dots[i][2][1] * math.cos(math.radians(caps[n][2]))))
        m = len(idx)
        th = math.radians(caps[n][2])
        for j, i in enumerate(idx):
            sft = (j - (m - 1) / 2) * (DOT_GAP + 1.6)
            rid, nn, _ = dots[i]
            dots[i] = (rid, nn, (caps[n][0] - math.sin(th) * sft, caps[n][1] + math.cos(th) * sft))
        caps[n] = (caps[n][0], caps[n][1], caps[n][2], max(caps[n][3], (m * (DOT_GAP + 1.6)) / 2 + 3))
    for _, _, (x, y) in dots:
        obstacles.append(Point(x, y).buffer(5))
    for end, lst in end_leaf.items():
        ux, uy = lst[0][1]
        m = len(lst)
        for j in range(m):
            sft = (j - (m - 1) / 2) * 21
            obstacles.append(Point(P[end][0] + ux * 26 - uy * sft, P[end][1] + uy * 26 + ux * sft).buffer(11))

    # ---- labels
    placed, labels, leaders = [], [], []
    label_fail = 0
    dot_pts = collections.defaultdict(list)
    for _rid, _n, _pt in dots:
        dot_pts[_n].append(Point(_pt))
    all_dots = [(_n, pt) for _n, pts in dot_pts.items() for pt in pts]
    stations = sorted((n for n in P if not n.startswith("j:") and n != hub and tph.get(n)),
                      key=lambda n: -bundle[n])
    for n in stations:
        x, y = P[n]
        name = data.names.get(n, n)
        tw = 7.4 * (len(name) + len(fmt_tph(tph[n])) + 1) + 22 * len(end_label.get(n, []))
        half = bundle[n] + 12
        best = None
        th_n = L["dir"].get(n, 0.0)
        cand_list = []
        ab_n = end_edge.get(n)
        anchors = [(x, y)]
        if ab_n and ab_n in ext:
            dn_ = unit(P[ab_n[0]], P[ab_n[1]])
            mn_, mx_ = ext[ab_n]
            anchors = [(x + dn_[1] * mx_, y - dn_[0] * mx_)]
            if mx_ - mn_ > 1e-6:
                anchors.append((x + dn_[1] * mn_, y - dn_[0] * mn_))
        for extra in (0, 10, 20):
            for ax, ay in anchors:
                cand_list += nrm.candidates(ax, ay, 12 + extra, tw, (math.cos(th_n), math.sin(th_n)))
        for pref, (mode, poly, tx, ty, anchor, rot) in enumerate(cand_list):
            cost = pref * 2.0
            for ob in obstacles:
                if ob.intersects(poly):
                    cost += 1000 + ob.intersection(poly).area
            for pl in placed:
                if pl.intersects(poly):
                    cost += 1000 + pl.intersection(poly).area
            own = [poly.distance(pt) for pt in dot_pts.get(n, [])]
            d_own = min(own) if own else 0.0
            d_other = min((poly.distance(pt) for m, pt in all_dots if m != n), default=1e9)
            if d_own > LABEL_MAX:      # too far from its own station
                cost += 1000
            if d_other < d_own + 3:    # as close to another station: ambiguous
                cost += 1000
            if best is None or cost < best[0]:
                best = (cost, poly, tx, ty, anchor, rot)
        cost, poly, tx, ty, anchor, rot = best
        own_pts = dot_pts.get(n, [])
        if own_pts:  # a label that had to sit away from its dots gets a leader line to them
            dd, pt_ = min(((poly.distance(pt), pt) for pt in own_pts), key=lambda z: z[0])
            if dd > 12:
                q = nearest_points(poly, pt_)[0]
                leaders.append((q.x, q.y, pt_.x, pt_.y))
                cost = max(0.0, cost - 1000 * ((dd > LABEL_MAX) + 0))
        label_fail += cost >= 1000
        placed.append(poly)
        labels.append((n, tx, ty, anchor, rot, name, fmt_tph(tph[n])))
    hname = data.names.get(hub, hub)
    hw_ = 13.5 * (len(hname) + 5) * 0.8
    hbest = None
    hx, hy = P[hub]
    for pref, mode in enumerate(("R", "L", "U", "D")):
        mode, poly, tx, ty, anchor, rot = label_candidate(mode, hx, hy, hub_r + 8, hw_, 24.0)
        if rot:
            continue
        cost = pref
        for ob in obstacles:
            if ob is not pill and ob.intersects(poly):
                cost += 1000 + ob.intersection(poly).area
        for pl in placed:
            if pl.intersects(poly):
                cost += 1000 + pl.intersection(poly).area
        if hbest is None or cost < hbest[0]:
            hbest = (cost, tx, ty, anchor, poly)
    placed.append(hbest[4])

    # ---- rule report
    pts_all = [(d[1], d[2]) for d in dots]
    dot_overlap = 0
    where = {"dots": [], "cross": []}
    for i, a in enumerate(pts_all):
        for b in pts_all[i + 1:]:
            if math.hypot(a[1][0] - b[1][0], a[1][1] - b[1][1]) < DOT_GAP:
                dot_overlap += 1
                where["dots"].append((data.names.get(dots[0][1], ""), ))
    where["dots"] = []
    for i, a in enumerate(dots):
        for b in dots[i + 1:]:
            if math.hypot(a[2][0] - b[2][0], a[2][1] - b[2][1]) < DOT_GAP:
                where["dots"].append((data.names.get(a[1], a[1]), a[0], b[0], round(math.hypot(a[2][0] - b[2][0], a[2][1] - b[2][1]), 1)))
    lines = collections.defaultdict(list)
    for rid, _, pts in paths:
        lines[rid].append(LineString(pts))
    centres = [(Point(P[hub]), hub_r * 1.5)] + [(Point(P[n]), caps[n][3] + 10) for n in caps]
    crossings = 0
    ids = list(lines)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            for la in lines[a]:
                for lb in lines[b]:
                    if not la.crosses(lb):  # touching at a shared vertex is not a crossing
                        continue
                    x = la.intersection(lb)
                    if x.is_empty:
                        continue
                    for p in ([x] if x.geom_type == "Point" else
                              [g for g in getattr(x, "geoms", []) if g.geom_type == "Point"]):
                        if all(p.distance(c) > rad for c, rad in centres):
                            crossings += 1
                            nn = min((n for n in P if not n.startswith("j:")), key=lambda n: p.distance(Point(P[n])))
                            where["cross"].append((a, b, data.names.get(nn, nn), round(p.distance(Point(P[nn]))), round(p.x), round(p.y)))
    # lines of different routes running on top of each other (closer than a lane gap) for any real length
    branch_overlap = 0
    keep_out = [c.buffer(rad) for c, rad in centres]
    thin = {rid: [ln.buffer(2.4) for ln in lns] for rid, lns in lines.items()}
    for i, a_ in enumerate(ids):
        for b_ in ids[i + 1:]:
            area = 0.0
            for ga in thin[a_]:
                for gb in thin[b_]:
                    if ga.intersects(gb):
                        x = ga.intersection(gb)
                        for k in keep_out:
                            x = x.difference(k)
                        area += x.area
            if area > 60:
                branch_overlap += 1
                where.setdefault("branch", []).append((a_, b_, round(area)))
    incident = collections.defaultdict(set)
    for ab, _, _ in edge_geoms:
        for n in ab:
            incident[n].add(ab)
    dot_on_line = 0
    for n in {d[1] for d in dots}:
        near_nodes = {x for ab in incident[n] for x in ab} | {"j:" + n}
        for ab, ls, w in edge_geoms:
            if ab[0] in near_nodes or ab[1] in near_nodes:
                continue
            if ls.distance(Point(P[n])) < w - 1:
                dot_on_line += 1
                where["dots"].append((data.names.get(n, n), "on a line", ab))
    report = {"routes": len(routes), "dot_overlap": dot_overlap, "label_collision": int(label_fail),
              "crossings": crossings, "branch_overlap": branch_overlap, "dot_on_line": dot_on_line,
              "leader_lines": len(leaders)}
    # every rule failure counts fully; each leader line (a label that could not sit by its dots) counts a little
    failures = dot_overlap + int(label_fail) + crossings + branch_overlap + dot_on_line + 0.15 * len(leaders)

    # ---- svg
    xs = [p[0] for p in P.values()] + [q.bounds[0] for q in placed] + [q.bounds[2] for q in placed]
    ys = [p[1] for p in P.values()] + [q.bounds[1] for q in placed] + [q.bounds[3] for q in placed]
    x0, x1, y0, y1 = min(xs) - 60, max(xs) + 60, min(ys) - 60, max(ys) + 60
    head, leg_h = 150, 70 + 27 * len(routes)
    W = max(x1 - x0, 840)
    H = y1 - y0 + head + leg_h + 40
    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s %s %s %s" font-family="Helvetica Neue, '
         'Helvetica, Arial, sans-serif">' % (f(x0), f(y0 - head), f(W), f(H)),
         '<rect x="%s" y="%s" width="%s" height="%s" fill="#fff"/>' % (f(x0), f(y0 - head), f(W), f(H)),
         '<text x="%s" y="%s" font-size="56" font-weight="300" fill="#c2185b">%s Routes</text>'
         % (f(x0 + 30), f(y0 - head + 70), esc(hname)),
         '<text x="%s" y="%s" font-size="19" fill="#444">Normal daytime service pattern: average trains per hour '
         '(tph) each way, Tuesday&#8211;Thursday %02d:00&#8211;%02d:00, September 2026</text>'
         % (f(x0 + 32), f(y0 - head + 102), WINDOW[0], WINDOW[1]),
         '<text x="%s" y="%s" font-size="14" fill="#777">Source: National Rail Darwin Push Port, services that ran. '
         'Routes are drawn to their termini; stations no route here stops at are left out.</text>'
         % (f(x0 + 32), f(y0 - head + 126)),
         '<text x="%s" y="%s" font-size="14" fill="#777">Number beside a station: tph calling. White dot: the route '
         'stops. Capsule: major interchange between routes going different ways.</text>'
         % (f(x0 + 32), f(y0 - head + 145))]
    for rid, d, _ in paths:
        o.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round"/>'
                 % (d, colours[rid], LINE_W))
    for lx1, ly1, lx2, ly2 in leaders:
        o.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="#777" stroke-width="1"/>'
                 % (f(lx1), f(ly1), f(lx2), f(ly2)))
    o.append('<rect x="%s" y="%s" width="%s" height="%s" rx="15" fill="#fff" stroke="#111" stroke-width="3.5"/>'
             % (f(hx - hub_r), f(hy - hub_r), f(2 * hub_r), f(2 * hub_r)))
    for n, (cx, cy, deg, hw) in caps.items():
        o.append('<rect x="%s" y="%s" width="16" height="%s" rx="8" fill="#fff" stroke="#111" stroke-width="2.4" '
                 'transform="rotate(%s %s %s)"/>' % (f(cx - 8), f(cy - hw), f(2 * hw), f(deg), f(cx), f(cy)))
    for rid, n, (x, y) in dots:
        o.append('<circle cx="%s" cy="%s" r="%s" fill="#fff" stroke="#111" stroke-width="1.5"/>' % (f(x), f(y), DOT_R))
    _, htx, hty, hanchor, _ = hbest
    o.append('<text x="%s" y="%s" font-size="22" font-weight="700" fill="#111" text-anchor="%s" stroke="#fff" '
             'stroke-width="5" paint-order="stroke">%s <tspan fill="#c2185b" font-size="15">%s</tspan></text>'
             % (f(htx), f(hty), hanchor, esc(hname), fmt_tph(sum(r["tph"] for r in routes))))
    for n, tx, ty, anchor, rot, name, t in labels:
        tr = ' transform="rotate(%d %s %s)"' % (rot, f(tx), f(ty)) if rot else ""
        o.append('<text x="%s" y="%s" font-size="%s" fill="#1a1a1a" text-anchor="%s"%s><tspan font-weight="500">%s'
                 '</tspan><tspan dx="6" font-size="12" fill="#c2185b" font-weight="700">%s</tspan></text>'
                 % (f(tx), f(ty), FONT, anchor, tr, esc(name), t))
        for j, rid in enumerate(end_label.get(n, [])):
            w = 7.4 * (len(name) + len(t) + 1) + 18 + 20 * j
            a = math.radians(rot)
            sg = 1 if anchor == "start" else -1
            o.append(badge(colours[rid], rid, tx + sg * w * math.cos(a), ty + sg * w * math.sin(a) - 4, 8.5))
    for end, lst in end_leaf.items():
        ux, uy = lst[0][1]
        m = len(lst)
        for j, (rid, _) in enumerate(lst):
            sft = (j - (m - 1) / 2) * 21
            o.append(badge(colours[rid], rid, P[end][0] + ux * 26 - uy * sft, P[end][1] + uy * 26 + ux * sft, 9.5))
    o.append(legend(routes, colours, data, x0 + 30, y1 + 30))
    o.append("<!-- spacing %s report %s -->" % (spacing, {k: v for k, v in report.items()}))
    o.append("</svg>")
    report["where"] = where
    return "\n".join(o), report, failures


def build_map(data, nr, hub, verbose=True):
    base.K_STOPS = 999          # routes are drawn in full
    base.MAX_ROUTES = MAX_ROUTES
    routes = discover_routes(data, hub)
    if not routes:
        return None
    expand_via_pdf(routes, nr, data)
    results = []
    for sp in SPACINGS:
        svg, rep, fail = render(data, hub, routes, sp)
        results.append((sp, fail, rep, svg))
        if verbose:
            print("   spacing %3d failures %d %s" % (sp, fail, {k: v for k, v in rep.items() if k != "where"}))
    def hard(r):  # rule failures, ignoring the small leader-line charge
        return int(r[1])
    best = min(hard(r) for r in results)  # fewest hard failures first, then the tightest spacing
    chosen = min((r for r in results if hard(r) == best), key=lambda r: r[0])
    return chosen[3], routes, chosen[0], chosen[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stations", nargs="+")
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "maps"))
    ap.add_argument("--network", default=None, help="nr_schematic.json from extract_nr_schematic.py (optional)")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    data = Data(a.patterns, a.tiplocs)
    nr = load_network(a.network, data)
    for crs in a.stations:
        res = build_map(data, nr, crs, verbose=a.v)
        if res is None:
            print(crs, "no routes")
            continue
        svg, routes, sp, rep = res
        open(os.path.join(a.out, crs + ".svg"), "w").write(svg)
        print(crs, data.names.get(crs), "spacing", sp, {k: v for k, v in rep.items() if k != "where"})
        if a.v:
            print("   dots:", rep["where"]["dots"][:6])
            print("   crossings:", rep["where"]["cross"][:10])


if __name__ == "__main__":
    main()
