"""Loops (two corridors joining the same places) as series-parallel blocks.

Pure functions on an edge list; no drawing. See plans/BETTER_LOOPS.md.

    blocks(edges)      2-edge-connected components with at least one cycle (the places that hold loops)
    reduce_block(edges) series-parallel reduction of one block: reducible or the irreducible core left over
"""
import collections


def _adjacency(edges):
    adj = collections.defaultdict(set)
    for a, b in edges:
        if a != b:
            adj[a].add(b)
            adj[b].add(a)
    return adj


def bridges(adj):
    """Edges whose removal disconnects the graph (iterative lowlink, so deep corridors are safe)."""
    seen, low, out = {}, {}, set()
    for s in adj:
        if s in seen:
            continue
        seen[s] = low[s] = len(seen)
        stack = [(s, None, iter(adj[s]))]
        while stack:
            u, par, it = stack[-1]
            for v in it:
                if v == par:
                    continue
                if v in seen:
                    low[u] = min(low[u], seen[v])
                else:
                    seen[v] = low[v] = len(seen)
                    stack.append((v, u, iter(adj[v])))
                    break
            else:
                stack.pop()
                if stack:
                    p = stack[-1][0]
                    low[p] = min(low[p], low[u])
                    if low[u] > seen[p]:
                        out.add(frozenset((p, u)))
    return out


def blocks(edges):
    """Each block is the list of station-pair edges of one 2-edge-connected component with a cycle."""
    adj = _adjacency(edges)
    cut = bridges(adj)
    keep = {frozenset(e) for e in edges if e[0] != e[1]} - cut
    nbr = collections.defaultdict(set)
    for e in keep:
        a, b = tuple(e)
        nbr[a].add(b)
        nbr[b].add(a)
    comp, out = {}, []
    for s in nbr:
        if s in comp:
            continue
        comp[s], todo, nodes = len(out), [s], {s}
        while todo:
            u = todo.pop()
            for v in nbr[u]:
                if v not in comp:
                    comp[v] = len(out)
                    nodes.add(v)
                    todo.append(v)
        out.append([tuple(sorted(e)) for e in keep if next(iter(e)) in nodes])
    return out


def reduce_block(edges):
    ok, core = reduce_core(edges)
    return ok, (len(core) if not ok else 0)


def reduce_core(edges):
    """Series-parallel reduction. Returns (True, 0) if the block collapses completely, else
    (False, core) where core is the stations left in the irreducible core (a ladder, K4, ...).

    Rules, applied until nothing changes: parallel edges between the same two stations merge into one;
    a station with two distinct neighbours is merged into a series edge; a station with one neighbour
    (what is left of a loop that returns to a shared station) is dropped."""
    m = collections.defaultdict(collections.Counter)
    for a, b in edges:
        if a != b:
            m[a][b] += 1
            m[b][a] += 1
    todo = list(m)
    while todo:
        u = todo.pop()
        if u not in m:
            continue
        for v in list(m[u]):
            if m[u][v] > 1:             # parallel: one edge stands for the bundle
                m[u][v] = m[v][u] = 1
                todo += [u, v]
        deg = sum(m[u].values())
        if deg == 1:
            (v,) = m[u]
            del m[u], m[v][u]
            todo.append(v)
        elif deg == 2 and len(m[u]) == 2:
            a, b = m[u]
            del m[u], m[a][u], m[b][u]
            m[a][b] += 1
            m[b][a] += 1
            todo += [a, b]
    core = [u for u in m if m[u]]
    return len(core) <= 2, (core if len(core) > 2 else [])
