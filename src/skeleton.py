"""Skeleton of a station's network: junction stations and the corridors between them.

A corridor is a maximal chain of stations that every route passes in the same order: each inner station has
exactly two neighbours in the station graph. Junctions are the stations with any other number of neighbours,
plus the hub. A corridor may join a junction to itself (a balloon loop) and two corridors may join the same
two junctions (a loop with no station of its own in between). Corridors whose edges lie on a cycle belong to
a block (see loops.blocks); the others are tree corridors and are drawn as spokes.

Pure functions on the routes' station paths; nothing here draws. See plans/BLOCK_LAYOUT.md.
"""
import collections
import math

import loops


def station_graph(routes):
    """Neighbours of every station and the tph over every edge (consecutive stops of the drawn routes)."""
    adj = collections.defaultdict(set)
    tph = collections.Counter()
    for r in routes:
        for a, b in zip(r["path"], r["path"][1:]):
            if a != b:
                adj[a].add(b)
                adj[b].add(a)
                tph[frozenset((a, b))] += r["tph"]
    return adj, tph


def bfs_parent(adj, tph, hub):
    """Parent and depth of every station on a busiest-first breadth-first walk from the hub."""
    parent, depth = {hub: None}, {hub: 0}
    q = collections.deque([hub])
    while q:
        u = q.popleft()
        for v in sorted(adj[u], key=lambda v: (-tph[frozenset((u, v))], v)):
            if v not in parent:
                parent[v], depth[v] = u, depth[u] + 1
                q.append(v)
    return parent, depth


class Skeleton:
    """nodes: the junction stations. corridors: dicts with id, a, b, stations (a ... b), block (index or None),
    routes (ids that use any of its edges). ends[v]: (corridor id, end) pairs at v, end 0 at a and 1 at b.
    bearing[(cid, end)]: compass direction (SVG frame, y down) the corridor leaves that end in, or None."""

    def __init__(self, routes, hub, xy):
        self.hub = hub
        self.adj, self.tph = station_graph(routes)
        if hub not in self.adj:
            self.adj[hub] = set()
        self.parent, self.depth = bfs_parent(self.adj, self.tph, hub)
        edges = [tuple(sorted(e)) for e in self.tph]
        self.block_of_edge = {}
        for i, bl in enumerate(loops.blocks(edges)):
            for e in bl:
                self.block_of_edge[frozenset(e)] = i
        self.nodes = {v for v, nb in self.adj.items() if len(nb) != 2} | {hub}
        self.corridors = []
        self.ends = collections.defaultdict(list)
        used = set()
        for v in sorted(self.nodes, key=lambda n: (self.depth.get(n, 1e9), n)):
            for x in sorted(self.adj[v], key=lambda n: (self.depth.get(n, 1e9), n)):
                if frozenset((v, x)) in used:
                    continue
                chain, prev, cur = [v, x], v, x
                used.add(frozenset((v, x)))
                while cur not in self.nodes:
                    (nxt,) = [n for n in self.adj[cur] if n != prev] or [prev]
                    if frozenset((cur, nxt)) in used:
                        break
                    used.add(frozenset((cur, nxt)))
                    chain.append(nxt)
                    prev, cur = cur, nxt
                cid = len(self.corridors)
                self.corridors.append({"id": cid, "a": chain[0], "b": chain[-1], "stations": chain,
                                       "block": self.block_of_edge.get(frozenset((v, x))), "routes": set()})
                self.ends[chain[0]].append((cid, 0))
                self.ends[chain[-1]].append((cid, 1))
        self.edge_corridor = {}
        for c in self.corridors:
            for a, b in zip(c["stations"], c["stations"][1:]):
                self.edge_corridor[frozenset((a, b))] = c["id"]
        for r in routes:
            for a, b in zip(r["path"], r["path"][1:]):
                if a != b:
                    self.corridors[self.edge_corridor[frozenset((a, b))]]["routes"].add(r["id"])
        self.bearing = {}
        for c in self.corridors:
            for end in (0, 1):
                self.bearing[(c["id"], end)] = self._bearing(c, end, xy)
        self.blocks = collections.defaultdict(set)   # block index -> its junction nodes
        for c in self.corridors:
            if c["block"] is not None:
                self.blocks[c["block"]] |= {c["a"], c["b"]}

    def _bearing(self, c, end, xy, depth=3):
        st = c["stations"] if end == 0 else c["stations"][::-1]
        if st[0] not in xy:
            return None
        ox, oy = xy[st[0]]
        vx = vy = 0.0
        for s in st[1:1 + depth]:
            if s in xy:
                dx, dy = xy[s][0] - ox, xy[s][1] - oy
                n = math.hypot(dx, dy) or 1.0
                vx, vy = vx + dx / n, vy + dy / n
        return math.atan2(vy, vx) if (vx or vy) else None

    def is_tree(self, cid):
        return self.corridors[cid]["block"] is None

    def other_end(self, cid, end):
        c = self.corridors[cid]
        return c["b"] if end == 0 else c["a"]

    def entry(self, block):
        """The junction through which the hub reaches a block: the hub itself, or its shallowest node."""
        nodes = self.blocks[block]
        return self.hub if self.hub in nodes else min(nodes, key=lambda n: (self.depth.get(n, 1e9), n))

    def block_order(self):
        """Blocks nearest the hub first."""
        return sorted(self.blocks, key=lambda b: (self.depth.get(self.entry(b), 1e9), b))
