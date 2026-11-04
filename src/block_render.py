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


def consistent_order(rs, future, past):
    """Left-to-right order of the routes on a corridor. Where routes go after the far junction orders them;
    where they came from before the near junction orders them too, as far as that agrees. A route that ends
    inside the corridor is unconstrained at the far end (and one that starts inside, at the near end); two
    such routes keep the order of their end points. A past constraint that contradicts the future ones is
    dropped: the crossing then sits at the near junction, as it must."""
    adj = {r: set() for r in rs}

    def constraints(key):
        """Pairs ordered by this end: only between routes that both continue beyond it."""
        out = []
        for a in rs:
            for b in rs:
                if a != b and not _ends(key[a]) and not _ends(key[b]) and key[a] < key[b]:
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
    return out + sorted(r for r in rs if r not in out)


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
        order[cid] = consistent_order(list(rs), {rid: keys[(cid, rid)][0] for rid in rs},
                                      {rid: keys[(cid, rid)][1] for rid in rs})

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
            if all(rid in po for rid in rs) and max(abs(po[rid]) for rid in rs) <= (n - 1) / 2 * PITCH + PITCH + 1e-6:
                offs[cid] = {rid: p[1] * po[rid] for rid in rs}
                return offs[cid]
        offs[cid] = {rid: ((n - 1) / 2 - i) * PITCH for i, rid in enumerate(rs)}
        return offs[cid]

    for cid in order:
        solve(cid)
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
    scene = {"P": P, "hub_r": G["hub_r"], "off": off, "ext": ext, "lanes": lanes, "end_edge": end_edge,
             "is_leaf": lambda n: n in G["leaf"], "dirs": G["tangent"], "retrace": retrace,
             "n_loops": sum(1 for c in sk.corridors if c["block"] is not None),
             "junction_centres": [(n, bundle_r[n]) for n in G["junctions"] | {hub}]}
    global LAST
    LAST = {"keys": keys, "order": order, "offs": offs, "eoffs": eoffs, "ost": ost, "pred": pred, "G": G}
    svg, report, failures = hm.draw_scene(data, hub, routes, scene, spacing, omitted)
    report["blocks"] = {b: dict(info) for b, info in G["blocks"].items()}
    return svg, report, failures
