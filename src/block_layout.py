"""Lay a station's network out on its skeleton: blocks as octilinear graphs, the rest as spokes.

    routes -> skeleton (junctions + corridors) -> each block: directions at every junction from the compass
    order of its corridors, a linear program for the junction positions and segment lengths (every corridor
    closes on its two junctions, so every cycle closes; every corridor is long enough for its stations), with
    separation constraints added where two corridors come out too close -> stations spaced evenly along
    their corridor -> tree corridors as straight spokes, as the tree layout draws them.

Returns a drawn graph: positions of stations and bend waypoints, and the drawn path of every station step.
See plans/BLOCK_LAYOUT.md.
"""
import collections
import itertools
import math

import numpy as np
from scipy.optimize import linprog

import station_route_map as base
from station_route_map import angdiff, assign_ordered
import skeleton as skel

OCT = [k * math.pi / 4 for k in range(8)]
MIN_STEP = 0.75        # stations on a corridor are at least this fraction of SPACING apart
END_MIN = 0.5          # a corridor's first and last segment are at least this fraction of SPACING (shows the direction)
SEP_GAP = 16.0         # clear space between the lane bundles of two corridors
NODE_R = 14.0          # clear space around a junction that a corridor not using it must keep
ROUNDS = 8             # rounds of separation constraints per layout
BUDGET = 1200          # layout evaluations per block in the direction search (the final map; probes use fewer)
ALTERNATIVES = 20      # direction assignments tried per junction per pass
W_CROSS, W_OVERLAP, W_SLACK, W_BEND, W_DEV, W_LEN = 100.0, 30.0, 4.0, 1.5, 1.0, 0.002
STUB = 1.6             # a spoke leaving a block junction keeps this many SPACINGs clear (for separation only)
CORNER_KEEP = 15.0     # stations keep this far from a bend
J_DIST = 34.0          # junction waypoint distance beyond a tree junction (as station_route_map.place)

_CACHE = {}            # skeleton signature -> chosen directions, so the spacing search does not repeat the search
_WARM = {}             # (corridor stations, end) -> last chosen direction; (corridor stations,) -> middle: a warm start
                       # for the next skeleton of the same station (the route picker adds one route at a time)


def vec(th):
    return (math.cos(th), math.sin(th))


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def snap(th):
    return min(OCT, key=lambda a: angdiff(a, th))


def ordered_assignments(items, want, allowed, theta_in):
    """Every assignment of items to distinct allowed directions that keeps their angular order, cheapest
    first (deviation from where each really goes, plus a little for turning back towards theta_in)."""
    k, n = len(items), len(allowed)
    if k > n or not items:
        return []
    cyclic = theta_in is None
    key = (lambda c: want[c] % (2 * math.pi)) if cyclic else (lambda c: wrap(want[c] - theta_in))
    ks = sorted(items, key=key)
    out = []
    for combo in itertools.combinations(range(n), k):
        for r in (range(k) if cyclic else (0,)):
            cost = 0.0
            asg = {}
            for i, c in enumerate(ks):
                a = allowed[combo[(i + r) % k]]
                asg[c] = a
                cost += angdiff(want[c], a) + (0.0 if cyclic else 0.25 * angdiff(a, theta_in))
            out.append((cost, asg))
    out.sort(key=lambda z: z[0])
    return [a for _, a in out]


class BlockLP:
    """Positions of a block's junctions and the lengths of its corridors' segments, for a given choice of
    directions. Variables: x, y per junction; three lengths per corridor (out, middle, in); a slack pair per
    corridor coordinate, so the program is always feasible and a bad choice of directions shows as slack."""

    def __init__(self, sk, block, dirs, mids, fixed, lanes, spacing, hub_gap, hub_r):
        self.sk, self.block = sk, block
        self.nodes = sorted(sk.blocks[block])
        self.cors = [c for c in sk.corridors if c["block"] == block]
        self.ni = {v: i for i, v in enumerate(self.nodes)}
        n, m = len(self.nodes), len(self.cors)
        self.nv = 2 * n + 3 * m + 4 * m
        self.lanes, self.spacing = lanes, spacing
        self.width = {c["id"]: max(1, lanes.get(c["id"], 1)) * base.PITCH for c in self.cors}
        self.step = MIN_STEP * spacing
        self.segdir = {}          # cid -> [th0, th1 or None, th2] directions of travel a -> b
        self._eq = []
        rows, rhs = [], []
        lo = np.zeros(self.nv)
        hi = np.full(self.nv, np.inf)
        cost = np.zeros(self.nv)
        for v in self.nodes:
            i = self.ni[v]
            if v in fixed:
                lo[2 * i], hi[2 * i] = fixed[v][0], fixed[v][0]
                lo[2 * i + 1], hi[2 * i + 1] = fixed[v][1], fixed[v][1]
            else:
                lo[2 * i] = lo[2 * i + 1] = -np.inf
        for j, c in enumerate(self.cors):
            cid = c["id"]
            th0 = dirs[(cid, 0)]
            th2 = (dirs[(cid, 1)] + math.pi) % (2 * math.pi)
            thm = mids.get(cid)
            self.segdir[cid] = [th0, thm, th2]
            L = 2 * n + 3 * j
            S = 2 * n + 3 * m + 4 * j
            cost[L:L + 3] = W_LEN * 1.0
            cost[S:S + 4] = W_SLACK
            inner = len(c["stations"]) - 2
            gap = [self._end_gap(c["stations"][0], hub_gap, hub_r, cid), self._end_gap(c["stations"][-1], hub_gap, hub_r, cid)]
            lo[L] = max(END_MIN * spacing, gap[0])
            lo[L + 2] = max(END_MIN * spacing, gap[1])
            if thm is None:
                hi[L + 1] = 0.0
            need = gap[0] + gap[1] + (inner + 1) * self.step
            r = np.zeros(self.nv)
            r[L:L + 3] = -1.0
            rows.append((r, -need))        # -(l0+l1+l2) <= -need
            ia, ib = self.ni[c["a"]], self.ni[c["b"]]
            for k in (0, 1):
                e = np.zeros(self.nv)
                e[2 * ib + k] += 1.0
                e[2 * ia + k] -= 1.0
                for s, th in enumerate(self.segdir[cid]):
                    if th is not None:
                        e[L + s] -= vec(th)[k]
                e[S + 2 * k] -= 1.0
                e[S + 2 * k + 1] += 1.0
                self._eq.append(e)
        self.A_eq = np.array(self._eq) if self._eq else None
        self.b_eq = np.zeros(len(self._eq)) if self._eq else None
        self.ub_rows = [r for r, _ in rows]
        self.ub_rhs = [b for _, b in rows]
        self.bounds = list(zip(lo, hi))
        self.cost = cost
        # points of every segment end as linear maps (2 x nv) of the variables
        self.segments = []
        for j, c in enumerate(self.cors):
            cid = c["id"]
            L = 2 * n + 3 * j
            M = np.zeros((2, self.nv))
            M[0, 2 * self.ni[c["a"]]] = 1.0
            M[1, 2 * self.ni[c["a"]] + 1] = 1.0
            for s, th in enumerate(self.segdir[cid]):
                if th is None:
                    continue
                M2 = M.copy()
                M2[0, L + s] += vec(th)[0]
                M2[1, L + s] += vec(th)[1]
                self.segments.append({"cid": cid, "k": s, "p": M, "q": M2, "th": th, "w": self.width[cid],
                                      "ends": {c["a"], c["b"]}, "var": L + s})
                M = M2
        # junctions as points, and spoke stubs leaving them, both for separation only
        self.points = []
        for v in self.nodes:
            M = np.zeros((2, self.nv))
            M[0, 2 * self.ni[v]] = 1.0
            M[1, 2 * self.ni[v] + 1] = 1.0
            rad = hub_r * 1.2 if v == sk.hub else NODE_R
            self.points.append({"node": v, "p": M, "r": rad})
            for cid, end in sk.ends[v]:
                if sk.is_tree(cid) and (cid, end) in dirs:
                    th = dirs[(cid, end)]
                    M2 = M.copy()
                    self.segments.append({"cid": -1 - cid, "k": 0, "p": M, "q": M2, "th": th, "stub": STUB * spacing,
                                          "w": max(1, lanes.get(cid, 1)) * base.PITCH, "ends": {v}, "var": None})
        self.extra = []       # separation rows (row, rhs)

    def _end_gap(self, v, hub_gap, hub_r, cid):
        if v == self.sk.hub:
            return hub_r + hub_gap + 2 * max(1, self.lanes.get(cid, 1))
        return 0.0

    def solve(self):
        A_ub = np.array(self.ub_rows + [r for r, _ in self.extra])
        b_ub = np.array(self.ub_rhs + [b for _, b in self.extra])
        res = linprog(self.cost, A_ub=A_ub, b_ub=b_ub, A_eq=self.A_eq, b_eq=self.b_eq, bounds=self.bounds,
                      method="highs")
        return res.x if res.success else None

    def concrete(self, x):
        segs = []
        for s in self.segments:
            p = s["p"] @ x
            if s["var"] is None:
                q = p + np.array(vec(s["th"])) * s["stub"]
                length = s["stub"]
            else:
                q = s["q"] @ x
                length = x[s["var"]]
            segs.append(dict(s, pp=p, qq=q, length=length))
        pts = [dict(p, pp=p["p"] @ x) for p in self.points]
        for sg in segs:
            sg["at"] = {pt["node"] for pt in pts if pt["node"] in sg["ends"] and
                        (math.dist(pt["pp"], sg["pp"]) < 1e-6 or math.dist(pt["pp"], sg["qq"]) < 1e-6)}
            ids = sorted(self.ni[v] for v in sg["at"]) + [-1, -2]
            sg["a0"], sg["a1"] = ids[0], ids[1]
        # ordinal among the drawn (non-zero) segments of each corridor, so a missing middle keeps out and in adjacent
        count = collections.Counter()
        for sg in segs:
            if sg["length"] >= 1e-6:
                sg["ord"] = count[sg["cid"]]
                count[sg["cid"]] += 1
            else:
                sg["ord"] = -1
        return segs, pts

    def slack(self, x):
        n, m = len(self.nodes), len(self.cors)
        return float(x[2 * n + 3 * m:].sum())


def _seg_dist(p, q, r, s):
    """Distance between segments p-q and r-s, and whether they properly cross."""
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def pdist(a, b, c):
        ab = (b[0] - a[0], b[1] - a[1])
        L2 = ab[0] ** 2 + ab[1] ** 2
        t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((c[0] - a[0]) * ab[0] + (c[1] - a[1]) * ab[1]) / L2))
        return math.hypot(a[0] + ab[0] * t - c[0], a[1] + ab[1] * t - c[1])

    o1, o2, o3, o4 = orient(p, q, r), orient(p, q, s), orient(r, s, p), orient(r, s, q)
    if (o1 > 1e-9 and o2 < -1e-9 or o1 < -1e-9 and o2 > 1e-9) and (o3 > 1e-9 and o4 < -1e-9 or o3 < -1e-9 and o4 > 1e-9):
        return 0.0, True
    return min(pdist(p, q, r), pdist(p, q, s), pdist(r, s, p), pdist(r, s, q)), False


def _pt_seg(C, A, B):
    """Distance from points C to segments A-B (all (n, 2) arrays, row by row)."""
    AB = B - A
    L2 = (AB ** 2).sum(axis=1)
    t = np.where(L2 < 1e-12, 0.0, ((C - A) * AB).sum(axis=1) / np.where(L2 < 1e-12, 1.0, L2))
    t = np.clip(t, 0.0, 1.0)
    X = A + AB * t[:, None]
    return np.hypot(X[:, 0] - C[:, 0], X[:, 1] - C[:, 1])


def close_pairs(segs, req_of):
    """Pairs of segments that must keep apart and are closer than their requirement, or cross.
    Returns a list of (i, j, dist, crosses). Vectorised over all pairs."""
    n = len(segs)
    if n < 2:
        return []
    PP = np.array([s["pp"] for s in segs], dtype=float)
    QQ = np.array([s["qq"] for s in segs], dtype=float)
    L = np.array([s["length"] for s in segs])
    cid = np.array([s["cid"] for s in segs])
    ordn = np.array([s["ord"] for s in segs])
    a0 = np.array([s["a0"] for s in segs])
    a1 = np.array([s["a1"] for s in segs])
    W = np.array([s["w"] for s in segs])
    I, J = np.triu_indices(n, 1)
    ok = (L[I] >= 1e-6) & (L[J] >= 1e-6) & ~((cid[I] == cid[J]) & (np.abs(ordn[I] - ordn[J]) < 2))
    shared = ((a0[I] == a0[J]) & (a0[I] >= 0)) | ((a0[I] == a1[J]) & (a0[I] >= 0)) | \
             ((a1[I] == a0[J]) & (a1[I] >= 0)) | ((a1[I] == a1[J]) & (a1[I] >= 0))
    ok &= ~shared
    I, J = I[ok], J[ok]
    if not len(I):
        return []
    req = req_of(W[I], W[J])
    d = np.minimum.reduce([_pt_seg(PP[J], PP[I], QQ[I]), _pt_seg(QQ[J], PP[I], QQ[I]),
                           _pt_seg(PP[I], PP[J], QQ[J]), _pt_seg(QQ[I], PP[J], QQ[J])])

    def orient(A, B, C):
        return (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])

    o1, o2 = orient(PP[I], QQ[I], PP[J]), orient(PP[I], QQ[I], QQ[J])
    o3, o4 = orient(PP[J], QQ[J], PP[I]), orient(PP[J], QQ[J], QQ[I])
    cross = ((o1 > 1e-9) & (o2 < -1e-9) | (o1 < -1e-9) & (o2 > 1e-9)) & ((o3 > 1e-9) & (o4 < -1e-9) | (o3 < -1e-9) & (o4 > 1e-9))
    bad = cross | (d < req - 1e-6)
    return [(int(i), int(j), float(dd), bool(c)) for i, j, dd, c in zip(I[bad], J[bad], d[bad], cross[bad])]


def _separate(lp, segs, pts):
    """Add one separation constraint for every pair that is too close, on the side it is already on (the
    cheapest of lateral or axial separation in either segment's frame). Returns how many were added."""
    added = 0

    def candidates(s, t_p, t_q, req):
        """Constraint rows pushing the points t_p, t_q (linear maps) clear of segment s."""
        out = []
        d = np.array(vec(s["th"]))
        nrm = np.array([d[1], -d[0]])
        for sign in (1.0, -1.0):
            rows = []
            viol = 0.0
            for T, tt in ((t_p[0], t_p[1]), (t_q[0], t_q[1])):
                r = -sign * (nrm @ (T - s["p"]))      # -(sign * n.(t - p)) <= -req
                rows.append((r, -req))
                viol += max(0.0, req - sign * float(nrm @ (tt - s["pp"])))
            out.append((viol, rows))
        for sign, base_pt, base_cc in ((1.0, s["q"], s["qq"]), (-1.0, s["p"], s["pp"])):
            rows = []
            viol = 0.0
            for T, tt in ((t_p[0], t_p[1]), (t_q[0], t_q[1])):
                r = -sign * (d @ (T - base_pt))
                rows.append((r, -SEP_GAP))
                viol += max(0.0, SEP_GAP - sign * float(d @ (tt - base_cc)))
            out.append((viol + 1.0, rows))      # axial separation is a little less natural
        return out

    for i, j, _dist, _cross in close_pairs(segs, lambda wi, wj: wi / 2 + wj / 2 + SEP_GAP):
        s, t = segs[i], segs[j]
        req = s["w"] / 2 + t["w"] / 2 + SEP_GAP
        cands = candidates(s, (t["p"], t["pp"]), (t["q"], t["qq"]), req) + \
            candidates(t, (s["p"], s["pp"]), (s["q"], s["qq"]), req)
        _, rows = min(cands, key=lambda z: z[0])
        lp.extra += rows
        added += 1
    for pt in pts:
        for s in segs:
            if pt["node"] in s["ends"] or s["length"] < 1e-6:
                continue
            req = s["w"] / 2 + pt["r"]
            dist, _ = _seg_dist(s["pp"], s["qq"], pt["pp"], pt["pp"])
            if dist >= req - 1e-6:
                continue
            _, rows = min(candidates(s, (pt["p"], pt["pp"]), (pt["p"], pt["pp"]), req), key=lambda z: z[0])
            lp.extra += rows
            added += 1
    return added


def evaluate(sk, block, dirs, mids, fixed, lanes, spacing, hub_gap, hub_r, bearings):
    """Lay the block out for these directions; score = crossings, overlaps, slack, bends, deviation, length."""
    lp = BlockLP(sk, block, dirs, mids, fixed, lanes, spacing, hub_gap, hub_r)
    x = lp.solve()
    if x is None:
        return math.inf, None
    for _ in range(ROUNDS):
        segs, pts = lp.concrete(x)
        if not _separate(lp, segs, pts):
            break
        x2 = lp.solve()
        if x2 is None:
            break
        x = x2
    segs, pts = lp.concrete(x)
    cross = overlap = 0
    for _i, _j, _dist, c in close_pairs(segs, lambda wi, wj: (wi + wj) / 2):
        if c:
            cross += 1
        else:
            overlap += 1
    bends = 0.0      # a 45 degree bend counts 1, a right angle 4, a hairpin 9: sharp turns inside a corridor read badly
    for c in lp.cors:
        drawn = [s["th"] for s in segs if s["cid"] == c["id"] and s["length"] > 1e-6]
        bends += sum((angdiff(a, b) / (math.pi / 4)) ** 2 for a, b in zip(drawn, drawn[1:]) if angdiff(a, b) > 1e-9)
    dev = sum(angdiff(th, bearings[k]) for k, th in dirs.items() if bearings.get(k) is not None)
    score = W_CROSS * cross + W_OVERLAP * overlap + W_SLACK * lp.slack(x) + W_BEND * bends + W_DEV * dev \
        + W_LEN * float(sum(s["length"] for s in segs if s["var"] is not None))
    return score, (lp, x, {"cross": cross, "overlap": overlap, "slack": lp.slack(x), "bends": bends})


def block_directions(sk, block, entry, theta_in, back_end, bearings):
    """First choice of directions: at every junction of the block, its corridor ends in their compass order."""
    dirs = {}
    for v in sk.blocks[block]:
        ends = list(sk.ends[v])
        want = {e: (bearings[e] if bearings.get(e) is not None else 0.0) for e in ends}
        if v == entry and theta_in is not None:
            dirs[back_end] = (theta_in + math.pi) % (2 * math.pi)
            rest = [e for e in ends if e != back_end]
            allowed = [(theta_in + k * math.pi / 4) % (2 * math.pi) for k in (-2, -1, 0, 1, 2)]
            asg = assign_ordered(rest, want, allowed, theta_in % (2 * math.pi)) if rest else {}
            if asg is None:
                asg = _greedy(rest, want, allowed)
            dirs.update(asg)
        else:
            asg = assign_ordered(ends, want, OCT[:], None) if ends else {}
            if asg is None:
                asg = _greedy(ends, want, OCT[:])
            dirs.update(asg)
    return dirs


def _greedy(items, want, allowed):
    """More items than directions: nearest free direction each, then share."""
    taken, free = {}, list(allowed)
    for _, c, a in sorted((angdiff(want[c], a), c, a) for c in items for a in allowed):
        if c in taken or a not in free:
            continue
        taken[c] = a
        free.remove(a)
    for c in items:
        if c not in taken:
            taken[c] = min(allowed, key=lambda a: angdiff(want[c], a))
    return taken


def initial_mids(sk, block, dirs, xy):
    """A middle direction for a corridor whose ends do not line up: the compass direction from a to b."""
    mids = {}
    for c in sk.corridors:
        if c["block"] != block:
            continue
        cid = c["id"]
        th0, th2 = dirs[(cid, 0)], (dirs[(cid, 1)] + math.pi) % (2 * math.pi)
        a, b = c["stations"][0], c["stations"][-1]
        if a == b:
            mids[cid] = snap(th0 + math.pi / 2)
            continue
        if a in xy and b in xy:
            g = snap(math.atan2(xy[b][1] - xy[a][1], xy[b][0] - xy[a][0]))
        else:
            g = th0
        if angdiff(th0, th2) < 1e-9:
            mids[cid] = None
        elif angdiff(g, th0) > 1e-9 and angdiff(g, th2) > 1e-9:
            mids[cid] = g
        else:
            mids[cid] = None
    return mids


def _mid_ok(cid, m, dirs):
    """A middle direction is only worth trying if it differs from both end directions."""
    if m is None:
        return True
    th0, th2 = dirs[(cid, 0)], (dirs[(cid, 1)] + math.pi) % (2 * math.pi)
    return angdiff(m, th0) > 1e-9 and angdiff(m, th2) > 1e-9


def search_block(sk, block, entry, theta_in, back_end, fixed, lanes, spacing, hub_gap, hub_r, xy):
    bearings = sk.bearing
    key = (block, tuple(sorted((c["id"], tuple(c["stations"])) for c in sk.corridors)), round((theta_in or 0.0), 3),
           tuple(sorted(lanes.items())))
    if key in _CACHE:
        dirs, mids = _CACHE[key]
        score, res = evaluate(sk, block, dirs, mids, fixed, lanes, spacing, hub_gap, hub_r, bearings)
        if res is not None:
            return dirs, mids, res
    dirs = block_directions(sk, block, entry, theta_in, back_end, bearings)
    mids = initial_mids(sk, block, dirs, xy)
    best, res = evaluate(sk, block, dirs, mids, fixed, lanes, spacing, hub_gap, hub_r, bearings)
    warm = _warm_start(sk, block, dirs, mids, entry, theta_in, back_end)
    if warm is not None:
        s, r = evaluate(sk, block, warm[0], warm[1], fixed, lanes, spacing, hub_gap, hub_r, bearings)
        if s < best:
            best, res, dirs, mids = s, r, warm[0], warm[1]
    budget = BUDGET
    nodes = sorted(sk.blocks[block])
    cors = [c["id"] for c in sk.corridors if c["block"] == block]
    improved = True
    while improved and budget > 0:
        improved = False
        for v in nodes:
            ends = list(sk.ends[v])
            want = {e: (bearings[e] if bearings.get(e) is not None else 0.0) for e in ends}
            if v == entry and theta_in is not None:
                rest = [e for e in ends if e != back_end]
                allowed = [(theta_in + k * math.pi / 4) % (2 * math.pi) for k in (-2, -1, 0, 1, 2)]
                alts = ordered_assignments(rest, want, allowed, theta_in % (2 * math.pi))
            else:
                alts = ordered_assignments(ends, want, OCT[:], None)
            tried = 0
            for asg in alts:
                if all(dirs.get(e) == a for e, a in asg.items()):
                    continue
                trial = dict(dirs)
                trial.update(asg)
                tmids = dict(mids)
                fresh = initial_mids(sk, block, trial, xy)
                for cid, _end in ends:      # corridors touched by this junction start from a fresh middle
                    if cid in fresh:
                        tmids[cid] = fresh[cid]
                s, r = evaluate(sk, block, trial, tmids, fixed, lanes, spacing, hub_gap, hub_r, bearings)
                budget -= 1
                tried += 1
                if s < best - 1e-9:
                    best, res, dirs, mids = s, r, trial, tmids
                    improved = True
                    break
                if tried >= ALTERNATIVES or budget <= 0:
                    break
            if budget <= 0:
                break
        for cid in cors:
            for m in [None] + OCT:
                if m == mids.get(cid) or budget <= 0 or not _mid_ok(cid, m, dirs):
                    continue
                tmids = dict(mids)
                tmids[cid] = m
                s, r = evaluate(sk, block, dirs, tmids, fixed, lanes, spacing, hub_gap, hub_r, bearings)
                budget -= 1
                if s < best - 1e-9:
                    best, res, mids = s, r, tmids
                    improved = True
                    break
    _CACHE[key] = (dirs, mids)
    for e, th in dirs.items():
        _WARM[(tuple(sk.corridors[e[0]]["stations"]), e[1])] = th
    for cid, m in mids.items():
        _WARM[(tuple(sk.corridors[cid]["stations"]),)] = m
    return dirs, mids, res


def _warm_start(sk, block, dirs, mids, entry, theta_in, back_end):
    """The last chosen directions for the corridors that still exist (same stations), if any; the rest as given.
    Directions at a junction must stay distinct, else the warm start is dropped for that junction."""
    if not _WARM:
        return None
    wd, wm, hit = dict(dirs), dict(mids), 0
    for v in sk.blocks[block]:
        if v == entry and theta_in is not None:
            continue
        cand = {}
        for e in sk.ends[v]:
            k = (tuple(sk.corridors[e[0]]["stations"]), e[1])
            if k in _WARM:
                cand[e] = _WARM[k]
        if len(cand) == len(sk.ends[v]) and len({round(a, 6) for a in cand.values()}) == len(cand):
            wd.update(cand)
            hit += 1
    for c in sk.corridors:
        if c["block"] == block and (tuple(c["stations"]),) in _WARM:
            wm[c["id"]] = _WARM[(tuple(c["stations"]),)]
    return (wd, wm) if hit else None


# ------------------------------------------------------------------------ placing stations and spokes
def _along(poly, s):
    cum = [0.0]
    for p, q in zip(poly, poly[1:]):
        cum.append(cum[-1] + math.dist(p, q))
    for i in range(len(cum) - 1):
        if cum[i] - 1e-9 <= s <= cum[i + 1] + 1e-9:
            seg = cum[i + 1] - cum[i]
            r = 0.0 if seg < 1e-9 else (s - cum[i]) / seg
            p, q = poly[i], poly[i + 1]
            return (p[0] + (q[0] - p[0]) * r, p[1] + (q[1] - p[1]) * r), math.atan2(q[1] - p[1], q[0] - p[0]), i
    p, q = poly[-2], poly[-1]
    return poly[-1], math.atan2(q[1] - p[1], q[0] - p[0]), len(poly) - 2


def place_corridor(c, poly, s_start, s_end, G):
    """Stations of corridor c evenly along the polyline poly (from a to b), bends as waypoints."""
    cid, st = c["id"], c["stations"]
    cum = [0.0]
    for p, q in zip(poly, poly[1:]):
        cum.append(cum[-1] + math.dist(p, q))
    total = cum[-1]
    inner = st[1:-1]
    m = len(inner)
    step = (total - s_start - s_end) / (m + 1) if m else 0.0
    ts = [s_start + (i + 1) * step for i in range(m)]
    for i in range(m):
        for sv in cum[1:-1]:
            if abs(ts[i] - sv) < CORNER_KEEP:
                ts[i] = sv + (CORNER_KEEP if ts[i] >= sv else -CORNER_KEEP)
    names = {i: "j:w%d:%d" % (i, cid) for i in range(1, len(poly) - 1)}
    for i, nm in names.items():
        G["P"][nm] = poly[i]
    prev, prev_s = st[0], 0.0
    for s, sv in zip(inner, ts):
        pos, tan, _ = _along(poly, sv)
        G["P"][s] = pos
        G["tangent"][s] = tan
        G["edge_path"][(prev, s)] = [prev] + [names[i] for i in names if prev_s < cum[i] < sv] + [s]
        prev, prev_s = s, sv
    G["edge_path"][(prev, st[-1])] = [prev] + [names[i] for i in names if prev_s < cum[i] < total] + [st[-1]]
    if st[0] not in G["tangent"] and len(poly) > 1:
        G["tangent"][st[0]] = math.atan2(poly[1][1] - poly[0][1], poly[1][0] - poly[0][0])
    if st[-1] not in G["tangent"]:
        G["tangent"][st[-1]] = math.atan2(poly[-1][1] - poly[-2][1], poly[-1][0] - poly[-2][0])


def layout(routes, hub, data, verbose=False):
    """The drawn graph for these routes. Uses base.SPACING, base.HUB_GAP and base.PITCH like the tree layout."""
    spacing, hub_gap = base.SPACING, base.HUB_GAP
    xy = data.xy
    sk = skel.Skeleton(routes, hub, xy)
    lanes = {c["id"]: len(c["routes"]) for c in sk.corridors}
    hubn = max([lanes[cid] for cid, _ in sk.ends[hub]] or [1])
    hub_r = hubn * base.PITCH / 2 + 10
    G = {"P": {hub: (0.0, 0.0)}, "edge_path": {}, "tangent": {}, "skeleton": sk, "hub_r": hub_r,
         "dir": {}, "wedge": {}, "junctions": set(), "blocks": {}, "corridor_poly": {}}
    done_blocks, done_cor = set(), set()
    placed_dirs = {}   # node -> True once its ends have directions

    def solve(block, entry, theta_in, back_end):
        fixed = {entry: G["P"][entry]}
        dirs, mids, res = search_block(sk, block, entry, theta_in, back_end, fixed, lanes, spacing, hub_gap,
                                       hub_r, xy)
        lp, x, info = res
        G["blocks"][block] = info
        for v in lp.nodes:
            G["P"][v] = (float(x[2 * lp.ni[v]]), float(x[2 * lp.ni[v] + 1]))
            G["junctions"].add(v)
        for e, th in dirs.items():
            # keep the entry's directions in the frame of the spoke that reached it (wedges compare unwrapped angles)
            G["dir"][e] = th if (theta_in is None or e_node(e) != entry) else theta_in + wrap(th - theta_in)
        for v in lp.nodes:
            placed_dirs[v] = True
            _wedges(v)
        n = len(lp.nodes)
        for j, c in enumerate(lp.cors):
            cid = c["id"]
            L = 2 * n + 3 * j
            poly = [G["P"][c["a"]]]
            pt = np.array(poly[0])
            last = None
            for s, th in enumerate(lp.segdir[cid]):
                if th is None or x[L + s] < 1e-6:
                    continue
                pt = pt + np.array(vec(th)) * float(x[L + s])
                if last is not None and angdiff(last, th) < 1e-9:
                    poly[-1] = (float(pt[0]), float(pt[1]))     # same direction: extend, no bend
                else:
                    poly.append((float(pt[0]), float(pt[1])))
                last = th
            if math.dist(poly[-1], G["P"][c["b"]]) > 0.5:
                poly.append(G["P"][c["b"]])     # slack left: close with a last straight (visible as a skew)
            else:
                poly[-1] = G["P"][c["b"]]
            if len(poly) < 2:
                poly = [G["P"][c["a"]], G["P"][c["b"]]]
            G["corridor_poly"][cid] = poly
            gap = [lp._end_gap(c["a"], hub_gap, hub_r, cid), lp._end_gap(c["b"], hub_gap, hub_r, cid)]
            place_corridor(c, poly, gap[0], gap[1], G)
            done_cor.add(cid)
        done_blocks.add(block)

    def e_node(e):
        c = sk.corridors[e[0]]
        return c["a"] if e[1] == 0 else c["b"]

    def _wedges(v):
        """Each end at v may fan its branch out only between the directions of its neighbours at v."""
        ends = list(sk.ends[v])
        for e in ends:
            if e not in G["dir"]:
                continue
            a = G["dir"][e]
            lo, hi = -math.inf, math.inf
            for e2 in ends:
                if e2 is e or e2 == e or e2 not in G["dir"]:
                    continue
                rel = wrap(G["dir"][e2] - a)
                if rel < -1e-6:
                    lo = max(lo, a + rel)
                elif rel > 1e-6:
                    hi = min(hi, a + rel)
            G["wedge"][e] = (lo, hi)

    def assign_tree_node(v, theta_in, lo, hi, back_end):
        """Directions for the ends at a junction reached along a spoke, keeping their angular order."""
        ends = [e for e in sk.ends[v] if e != back_end]
        if back_end is not None:
            G["dir"][back_end] = theta_in + math.pi
        if not ends:
            placed_dirs[v] = True
            return
        want = {}
        for e in ends:
            b = sk.bearing.get(e)
            want[e] = b if b is not None else (theta_in if theta_in is not None else 0.0)
        if theta_in is None:
            allowed = OCT[:]
        else:
            full = [theta_in + k * math.pi / 4 for k in (-2, -1, 0, 1, 2)]
            allowed = [a for a in full if lo - 1e-9 <= a <= hi + 1e-9] or full
        taken = assign_ordered(ends, want, allowed, theta_in)
        if taken is None:
            taken = _greedy(ends, want, allowed)
        G["dir"].update(taken)
        placed_dirs[v] = True
        # wedges from the neighbours, then clipped to the wedge this junction itself sits in
        _wedges(v)
        for e in ends:
            wl, wh = G["wedge"][e]
            G["wedge"][e] = (max(wl, lo), min(wh, hi))

    def spoke(e):
        """Lay out tree corridor e = (cid, end) from its placed end as a straight spoke."""
        cid, end = e
        c = sk.corridors[cid]
        v = e_node(e)
        th = G["dir"][e]
        st = c["stations"] if end == 0 else c["stations"][::-1]
        nl = max(1, lanes[cid])
        pv = G["P"][v]
        if v == hub:
            first, jpt = hub_r + hub_gap + 2 * nl, None
        else:
            # a side branch leaves from a junction point beyond the station, so the dots there stay in line
            th_in = G.get("in_theta", {}).get(v)
            straight = th_in is not None and abs(wrap(th - th_in)) < 1e-6
            if th_in is not None and not straight and v not in G["junctions"]:
                J = J_DIST + 2.5 * nl
                jpt = "j:" + v
                if jpt not in G["P"]:
                    G["P"][jpt] = (pv[0] + math.cos(th_in) * J, pv[1] + math.sin(th_in) * J)
                first = 44 + 2.5 * nl
            else:
                jpt, first = None, spacing
        origin = G["P"][jpt] if jpt else pv
        prev = v
        for i, s in enumerate(st[1:]):
            d = first + i * spacing
            G["P"][s] = (origin[0] + math.cos(th) * d, origin[1] + math.sin(th) * d)
            G["tangent"][s] = th
            G["edge_path"][(prev, s)] = ([prev, jpt, s] if (jpt and i == 0) else [prev, s])
            prev = s
        G.setdefault("in_theta", {})[st[-1]] = th
        done_cor.add(cid)
        return st[-1], (cid, 1 - end)

    # ---- the hub
    hub_block = next((b for b, nodes in sk.blocks.items() if hub in nodes), None)
    if hub_block is not None:
        solve(hub_block, hub, None, None)
    else:
        assign_tree_node(hub, None, -math.inf, math.inf, None)
    G["tangent"].setdefault(hub, 0.0)
    # ---- everything else, breadth first over the skeleton
    q = collections.deque([hub])
    seen = {hub}
    while q:
        v = q.popleft()
        for e in list(sk.ends[v]):
            cid = e[0]
            if cid in done_cor:
                far = sk.other_end(*e)
                if far not in seen:
                    seen.add(far)
                    q.append(far)
                continue
            if not sk.is_tree(cid):
                continue    # a block corridor at a junction whose block is solved later (from its entry)
            if e not in G["dir"]:
                continue
            far, far_end = spoke(e)
            lo, hi = G["wedge"].get(e, (-math.inf, math.inf))
            th = G["dir"][e]
            if far in seen:
                continue
            seen.add(far)
            far_block = next((b for b, nodes in sk.blocks.items() if far in nodes), None)
            if far_block is not None and far_block not in done_blocks:
                solve(far_block, far, th, far_end)
            elif far not in placed_dirs:
                assign_tree_node(far, th, lo, hi, far_end)
            q.append(far)
    # anything unreached (should not happen): drop it on the hub
    for c in sk.corridors:
        for s in c["stations"]:
            G["P"].setdefault(s, (0.0, 0.0))
    G["leaf"] = {v for v in sk.nodes if len(sk.adj[v]) == 1}
    if verbose:
        for b, info in G["blocks"].items():
            print("   block %d: %s" % (b, info))
    return G
