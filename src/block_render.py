"""Draw a station's routes on the block layout (block_layout.layout).

Each route walks its real stations over the drawn graph. Lanes on a corridor are ordered by where each route
goes after the corridor's far junction (the signed turn at each junction ahead, compared lexicographically),
then by where it came from before the near junction; routes that end inside the corridor sit on the left,
earliest end first. Offsets are fixed per corridor and inherited from the corridor the routes arrive by, so
lines do not wiggle after a junction. Dots, capsules, labels, the rule report and the SVG come from
hub_map.draw_scene, shared with the tree layout.
"""
import collections
import math

import block_layout
import hub_map as hm
import station_route_map as base
from station_route_map import unit

PITCH = base.PITCH
LOOK_AHEAD = 6      # junctions considered when ordering lanes
END_KEY = -1e9      # key of a route that ends: sorts left of every turn
LAST = None         # the lane bookkeeping of the last render (for debugging)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _ends(key):
    return key[0] <= END_KEY + 1e6


def _before(ka, kb):
    """True if key ka orders its route left of kb's, False if right, None if neither: equal, or they first
    differ where one of the two routes has ended (it is free to sit either side there)."""
    for x, y in zip(ka, kb):
        if x == y:
            continue
        if x <= END_KEY + 1e6 or y <= END_KEY + 1e6:
            return None
        return x < y
    return None


def consistent_order(rs, future, past, stops=None):
    """Left-to-right order of the routes on a corridor. Where routes go after the far junction orders them;
    where they came from before the near junction orders them too, as far as that agrees. A route that ends
    inside the corridor is unconstrained at the far end (and one that starts inside, at the near end); two
    such routes keep the order of their end points. A past constraint that contradicts the future ones is
    dropped: the crossing then sits at the near junction, as it must. stops[r] (optional): the stations r calls
    at inside the corridor; a route left free by an end is then moved next to routes that stop where it does, so
    a station's dots sit side by side under one label (Streatham Common: the two stopping trains, not the two
    outer lanes of a bundle of fast ones)."""
    adj = {r: set() for r in rs}

    def constraints(key):
        """Pairs ordered by this end: only between routes that both continue beyond it."""
        out = []
        for a in rs:
            for b in rs:
                if a != b and not _ends(key[a]) and not _ends(key[b]) and _before(key[a], key[b]):
                    out.append((a, b))
        return out

    def reaches(a, b):
        stack, seen = [a], {a}
        while stack:
            x = stack.pop()
            if x == b:
                return True
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False

    for a, b in constraints(future):
        adj[a].add(b)
    for a, b in sorted(constraints(past)):
        if b not in adj[a] and not reaches(b, a):
            adj[a].add(b)
    indeg = {r: 0 for r in rs}
    for a in rs:
        for b in adj[a]:
            indeg[b] += 1
    out, avail = [], [r for r in rs if indeg[r] == 0]
    while avail:
        r = min(avail, key=lambda r: (future[r] if not _ends(future[r]) else past[r], future[r], past[r], r))
        avail.remove(r)
        out.append(r)
        for b in sorted(adj[r]):
            indeg[b] -= 1
            if indeg[b] == 0:
                avail.append(b)
    out = out + sorted(r for r in rs if r not in out)
    if stops:
        out = _group_stoppers(out, adj, stops, reaches)
    return out


def _group_stoppers(out, adj, stops, reaches):
    """Move each route to the allowed position (no ordering constraint broken) next to the most routes that stop
    at the same stations on this corridor, when that is more than it has now."""
    def score(order, i):
        r = order[i]
        return sum(1 for j in (i - 1, i + 1) if 0 <= j < len(order) and stops.get(order[j]) == stops[r])

    for r in list(out):
        if not stops.get(r):
            continue
        i = out.index(r)
        best, best_s = None, score(out, i)
        rest = out[:i] + out[i + 1:]
        for k in range(len(rest) + 1):
            if any(reaches(r, x) for x in rest[:k]) or any(reaches(y, r) for y in rest[k:]):
                continue
            trial = rest[:k] + [r] + rest[k:]
            sc = score(trial, k)
            if sc > best_s:
                best, best_s = trial, sc
        if best is not None:
            out = best
    return out


def render(data, hub, routes, spacing, hub_gap=80.0, loops=True, omitted=()):
    base.SPACING, base.HUB_GAP = float(spacing), float(hub_gap)
    G = block_layout.layout(routes, hub, data)
    sk, P, EP = G["skeleton"], G["P"], G["edge_path"]

    def step_path(a, b):
        if (a, b) in EP:
            return EP[(a, b)]
        return EP[(b, a)][::-1]

    # ---- the drawn nodes of every route; a fold-back is now only a real reversal in the timetable
    retrace = 0
    for r in routes:
        path = [p for i, p in enumerate(r["path"]) if i == 0 or p != r["path"][i - 1]]
        dn = [path[0]]
        for a, b in zip(path, path[1:]):
            dn += step_path(a, b)[1:]
        r["draw"], r["tree_nodes"] = dn, path
        retrace += sum(1 for i in range(1, len(path) - 1) if path[i - 1] == path[i + 1])

    # ---- corridors oriented away from the hub; drawn edges in that orientation
    depth = sk.depth
    ost = {}            # cid -> oriented station list
    for c in sk.corridors:
        st = c["stations"]
        ost[c["id"]] = st if depth.get(st[0], 1e9) <= depth.get(st[-1], 1e9) else st[::-1]
    canon, edges_of, cors_of_edge = {}, {}, collections.defaultdict(set)
    for cid, st in ost.items():
        seq = [st[0]]
        for a, b in zip(st, st[1:]):
            seq += step_path(a, b)[1:]
        edges_of[cid] = []
        for x, y in zip(seq, seq[1:]):
            ab = canon.get((x, y)) or (x, y)
            canon[(x, y)] = canon[(y, x)] = ab
            edges_of[cid].append(ab)
            cors_of_edge[ab].add(cid)

    def direction(x, y):
        return math.atan2(P[y][1] - P[x][1], P[y][0] - P[x][0])

    def turn_after(v, prev_node, seq):
        """Signed turn a route makes leaving station v: from the drawn edge into v (prev_node -> v) to the first
        drawn edge of seq (v, ...) that points another way. A side branch leaves from a waypoint just beyond
        the station, so the first edge out of v is straight on for every route; the turn is at the waypoint."""
        th_in = direction(prev_node, v)
        t = 0.0
        for a, b in zip(seq, seq[1:]):
            t = wrap(direction(a, b) - th_in)
            if abs(t) > 1e-6:
                break
        return t

    def turns(walk, start):
        """Turns at the junctions a route meets along walk[start:] (walk is a station list), END_KEY if it ends."""
        out, k = [], start
        while len(out) < LOOK_AHEAD:
            if k + 1 >= len(walk):
                out.append(END_KEY)
                break
            v = walk[k]
            if v in sk.nodes and 0 < k:
                before = step_path(walk[k - 1], v)[-2]
                out.append(round(turn_after(v, before, step_path(v, walk[k + 1])), 6))
            k += 1
        return tuple(out)

    # ---- lane order per corridor
    order, uses = {}, collections.defaultdict(dict)     # uses[cid][rid] = (i0, i1, forward walk)
    for r in routes:
        path = r["tree_nodes"]
        at = {}
        for i, s in enumerate(path):
            for a, b in ((path[i - 1], s) if i else (None, None), (s, path[i + 1]) if i + 1 < len(path) else (None, None)):
                if a is not None:
                    at.setdefault(sk.edge_corridor[frozenset((a, b))], []).append(i)
        for cid, idx in at.items():
            st = ost[cid]
            pos = {s: j for j, s in enumerate(st)}
            i0, i1 = min(idx), max(idx)
            fwd = pos[path[i0]] <= pos[path[i1]] if path[i0] != path[i1] else True
            walk = path if fwd else path[::-1]
            w0, w1 = (i0, i1) if fwd else (len(path) - 1 - i1, len(path) - 1 - i0)
            uses[cid][r["id"]] = (w0, w1, walk)
    keys = {}
    calls = {r["id"]: set(r["calls"]) for r in routes}
    for cid, rs in uses.items():
        st = ost[cid]
        for rid, (w0, w1, walk) in rs.items():
            if walk[w1] == st[-1] and w1 + 1 < len(walk):
                future = turns(walk, w1)
            else:
                future = (END_KEY + st.index(walk[w1]),)
            back = walk[::-1]
            b1 = len(walk) - 1 - w0
            if walk[w0] == st[0] and w0 > 0:
                past = tuple(-t if t != END_KEY else t for t in turns(back, b1))
            else:
                past = (END_KEY + (len(st) - st.index(walk[w0])),)
            keys[(cid, rid)] = (future, past)
        inner = set(st[1:-1])
        stops = {rid: frozenset(inner & calls[rid]) for rid in rs}
        order[cid] = consistent_order(list(rs), {rid: keys[(cid, rid)][0] for rid in rs},
                                      {rid: keys[(cid, rid)][1] for rid in rs}, stops)

    # ---- offsets: per corridor, inherited from the corridor every route on it arrives by, so a route keeps its
    # lateral position through a junction (the hub's corridors are centred, as in the tree layout). The offset
    # keeps its sign when the previous corridor's canonical direction runs on into this one, and flips when the
    # previous corridor is oriented away from the shared junction.
    offs = {}
    pred = {}
    for cid, rs in uses.items():
        st = ost[cid]
        froms = set()
        for rid, (w0, w1, walk) in rs.items():
            if walk[w0] == st[0] and w0 > 0 and st[0] != hub:
                froms.add(sk.edge_corridor[frozenset((walk[w0 - 1], walk[w0]))])
            else:
                froms.add(None)
        p = froms.pop() if len(froms) == 1 else None
        if p is not None and ost[p][0] == ost[p][-1]:
            p = None                    # a balloon: no single direction to carry on from
        pred[cid] = (p, 1.0 if (p is not None and ost[p][-1] == st[0]) else -1.0) if p is not None else None

    def arrival(cid, p):
        """Direction (unit vector) the routes of pred corridor p arrive at cid's first station in."""
        v, pst = ost[cid][0], ost[p[0]]
        if p[1] > 0 and pst[-1] == v and len(pst) > 1:
            a = step_path(pst[-2], v)[-2]
        elif p[1] < 0 and pst[0] == v and len(pst) > 1:
            a = step_path(v, pst[1])[1]
        else:
            return None
        return unit(P[a], P[v])

    def jog_shift(cid, p):
        """Sideways distance from the line the routes arrive on (through the junction) to the first segment of
        cid's corridor polyline that runs parallel to it, if that segment comes within two segments of the
        junction; None otherwise."""
        poly = G["corridor_poly"].get(cid)
        d = arrival(cid, p)
        if not poly or d is None or len(poly) < 3:
            return None
        v = ost[cid][0]
        if math.dist(poly[0], P[v]) > 0.5:
            poly = poly[::-1]
        if math.dist(poly[0], P[v]) > 0.5:
            return None
        nrm = (d[1], -d[0])
        for q0, q1 in list(zip(poly, poly[1:]))[1:3]:
            u = unit(q0, q1)
            if abs(u[0] - d[0]) < 1e-6 and abs(u[1] - d[1]) < 1e-6:
                return nrm[0] * (q0[0] - P[v][0]) + nrm[1] * (q0[1] - P[v][1])
        return None

    def close_gaps(o):
        """Inherited lanes with holes where other routes turned off: the longest unbroken run of lanes stays where
        it is and the rest close up beside it, keeping their order (one line moves over, not the whole bundle)."""
        rs_ = sorted(o, key=lambda r: -o[r])
        if len(rs_) < 2:
            return o
        runs, s = [], 0
        for i in range(1, len(rs_) + 1):
            if i == len(rs_) or abs((o[rs_[i - 1]] - o[rs_[i]]) - PITCH) > 1e-6:
                runs.append((i - s, s))
                s = i
        _, s = max(runs, key=lambda t: (t[0], -abs(o[rs_[t[1]]])))
        return {r: o[rs_[s]] - (i - s) * PITCH for i, r in enumerate(rs_)}

    def solve(cid, stack=()):
        if cid in offs:
            return offs[cid]
        rs = order[cid]
        n = len(rs)
        p = pred.get(cid)
        if p is not None and p[0] not in stack and p[0] in order:
            po = solve(p[0], stack + (cid,))
            # keep the inherited offsets only while the bundle stays about as compact as a centred one: the
            # layout kept corridors apart by their own lane counts, so a lone route cannot ride 20 px off its axis
            st = ost[cid]
            # ... except straight on through a plain station where a side branch leaves (its first drawn step has
            # no waypoint): re-centred there, the routes going on would jog sideways across the branch's lanes
            straight_on = st[0] not in G["junctions"] and st[0] != hub and len(st) > 1 and len(step_path(st[0], st[1])) == 2
            if not straight_on and st[0] != hub and len(st) > 1:
                # ... or straight on through a junction (the main line on through Clapham Junction while the Windsor
                # lines turn off): re-centred, the whole bundle would jog sideways there
                d_in = arrival(cid, p)
                first = step_path(st[0], st[1])
                d_out = unit(P[first[0]], P[first[1]])
                straight_on = d_in is not None and abs(d_in[0] - d_out[0]) < 1e-6 and abs(d_in[1] - d_out[1]) < 1e-6
            if all(rid in po for rid in rs) and (straight_on or max(abs(po[rid]) for rid in rs) <= (n - 1) / 2 * PITCH + PITCH + 1e-6):
                offs[cid] = close_gaps({rid: p[1] * po[rid] for rid in rs})
                return offs[cid]
            if all(rid in po for rid in rs):
                # a corridor that leaves the junction by a short jog onto a line parallel to the one its routes
                # arrive on (Streatham Hill beside Streatham Common at Balham): measured from that parallel line,
                # the routes' arriving lanes may already be compact, so they keep them and run straight on
                D = jog_shift(cid, p)
                if D is not None:
                    shifted = {rid: p[1] * po[rid] - D for rid in rs}
                    if max(abs(v) for v in shifted.values()) <= (n - 1) / 2 * PITCH + PITCH + 1e-6:
                        offs[cid] = shifted
                        return offs[cid]
                # re-centred, but in the order the routes arrive in, so they do not swap sides at the junction
                rs = sorted(rs, key=lambda rid: -p[1] * po[rid])
        offs[cid] = {rid: ((n - 1) / 2 - i) * PITCH for i, rid in enumerate(rs)}
        return offs[cid]

    for cid in order:
        solve(cid)
    # a side branch off a spoke leaves from a waypoint just beyond its station, straight on from the line it came
    # by: on that stub its routes keep the lanes they arrived in, and move to the branch's own lanes only as it
    # turns away (re-centred on the stub, they would cut across the routes going straight on, which keep theirs)
    stub = collections.defaultdict(dict)
    for cid, st in ost.items():
        p = pred.get(cid)
        if p is None or p[0] not in offs or cid not in offs or len(st) < 2:
            continue
        seq = step_path(st[0], st[1])
        if len(seq) != 3 or seq[1] != "j:" + str(st[0]):
            continue   # only the straight stub a spoke's side branch leaves by (not a corridor's own bend points)
        po = offs[p[0]]
        if all(rid in po for rid in offs[cid]):
            stub[canon[(seq[0], seq[1])]].update({rid: p[1] * po[rid] for rid in offs[cid]})
    # drawn edges: lanes and offsets; an edge shared by several corridors (a junction stub) takes them all
    lanes, eoffs = {}, {}
    for ab, cids in cors_of_edge.items():
        here = {}
        for cid in cids:
            if cid not in offs:
                continue
            st = ost[cid]
            for rid in order[cid]:
                w0, w1, walk = uses[cid][rid]
                seq = []
                for a, b in zip(walk[w0:w1 + 1], walk[w0 + 1:w1 + 1]):
                    seq += step_path(a, b)[1:] if seq else step_path(a, b)
                if any(canon.get((x, y)) == ab for x, y in zip(seq, seq[1:])):
                    here[rid] = offs[cid][rid]
        if len(cids) > 1 and here:
            # a shared stub: keep every route's offset from its corridor only if they do not collide
            vals = sorted(here.values())
            if any(b - a < PITCH - 1e-6 for a, b in zip(vals, vals[1:])):
                # order by the turn each corridor takes at the junction, left first
                rank = []
                for cid in cids:
                    for rid in order.get(cid, []):
                        if rid in here:
                            rank.append((keys[(cid, rid)], rid))
                rs = [rid for _, rid in sorted(rank)]
                n = len(rs)
                here = {rid: ((n - 1) / 2 - i) * PITCH for i, rid in enumerate(rs)}
        lanes[ab] = sorted(here, key=lambda rid: -here[rid])
        eoffs[ab] = here
    for ab, here in stub.items():
        vals = sorted(here.values())
        if set(here) == set(eoffs.get(ab, {})) and all(b - a >= PITCH - 1e-6 for a, b in zip(vals, vals[1:])):
            eoffs[ab] = here
            lanes[ab] = sorted(here, key=lambda rid: -here[rid])
    ext = {ab: (min(o.values()), max(o.values())) for ab, o in eoffs.items() if o}

    def off(rid, a, b):
        ab = canon[(a, b)]
        s = eoffs.get(ab, {}).get(rid)
        if s is None:
            s = ext.get(ab, (0.0, 0.0))[1] + PITCH
        d = unit(P[ab[0]], P[ab[1]])
        return (d[1] * s, -d[0] * s)

    # ---- the drawn edge that reaches every station (for capsules and label anchors)
    end_edge = {}
    for cid, st in ost.items():
        prev = st[0]
        for s in st[1:]:
            seq = step_path(prev, s)
            end_edge.setdefault(s, canon[(seq[-2], seq[-1])])
            prev = s
    for cid, st in ost.items():
        if st[0] not in end_edge and edges_of[cid]:
            end_edge[st[0]] = edges_of[cid][0]
    bundle_r = {}
    for n in G["junctions"] | {hub}:
        w = max([abs(v) for ab in cors_of_edge for v in ext.get(ab, (0, 0)) if n in ab] or [0.0])
        bundle_r[n] = w + PITCH + 10
    # two wide bundles leaving the hub 45 degrees apart cross just outside the pill: allowed there, but reported
    bundle_r[hub] = max(bundle_r[hub] * 2.2, G["hub_r"] * 1.5 + PITCH)
    # a junction's dots and capsule lie across its busiest line (York: across the main line, not the Harrogate
    # line that happened to place it first)
    for n in G["junctions"]:
        if n == hub:
            continue
        busiest = max(((len(o), str(ab), ab) for ab, o in eoffs.items() if n in ab and o), default=None)
        if busiest:
            a, b = busiest[2]
            G["tangent"][n] = math.atan2(P[b][1] - P[a][1], P[b][0] - P[a][0])
    scene = {"P": P, "hub_r": G["hub_r"], "off": off, "ext": ext, "lanes": lanes, "end_edge": end_edge,
             "is_leaf": lambda n: n in G["leaf"], "dirs": G["tangent"], "retrace": retrace,
             "n_loops": sum(1 for c in sk.corridors if c["block"] is not None),
             "junction_centres": [(n, bundle_r[n]) for n in G["junctions"] | {hub}]}
    global LAST
    LAST = {"keys": keys, "order": order, "offs": offs, "eoffs": eoffs, "ost": ost, "pred": pred, "G": G}
    svg, report, failures = hm.draw_scene(data, hub, routes, scene, spacing, omitted)
    report["blocks"] = {b: dict(info) for b, info in G["blocks"].items()}
    return svg, report, failures
