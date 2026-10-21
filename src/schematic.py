"""Schematic positions for a line graph: stations spaced by how many stops apart they are, not how far.

Real distances vary enormously on national rail (a London suburb, then 40 km of fields), so a map at true
scale is unreadable. Stress majorisation (SMACOF) places stations so that layout distance follows
graph distance, with an edge's length growing only slowly with its real length. It starts from the real
positions, so the overall orientation of the network is kept.
"""
import heapq
import math

import numpy as np


def edge_weight(metres, hop_m=3000.0, k=0.5):
    """Layout length of an edge, in units of one 'typical hop'."""
    return 1.0 + k * math.log1p(metres / hop_m)


def shortest_paths(n, edges):
    """All-pairs shortest path lengths over (a, b, w) edges on nodes 0..n-1."""
    adj = [[] for _ in range(n)]
    for a, b, w in edges:
        adj[a].append((b, w))
        adj[b].append((a, w))
    D = np.full((n, n), np.inf)
    for s in range(n):
        D[s, s] = 0.0
        heap = [(0.0, s)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > D[s, u]:
                continue
            for v, w in adj[u]:
                if d + w < D[s, v]:
                    D[s, v] = d + w
                    heapq.heappush(heap, (d + w, v))
    return D


def stress_layout(pos0, edges, anchor=0.0, iters=400, tol=1e-5):
    """pos0: (n, 2) real positions in metres; edges: (a, b, metres). Returns (n, 2) positions in hop units.
    anchor pulls every station towards its real position (as a share of the average stress weight), so the
    diagram stays true to the geography; 0 spaces stations purely by stops apart."""
    n = len(pos0)
    w_edges = [(a, b, edge_weight(m)) for a, b, m in edges]
    D = shortest_paths(n, w_edges)
    big = np.nanmax(np.where(np.isfinite(D), D, np.nan))
    D = np.where(np.isfinite(D), D, big + 5.0)       # separate components are placed apart, not on top
    W = np.zeros((n, n))
    mask = D > 0
    W[mask] = D[mask] ** -2.0
    X = np.array(pos0, dtype=float)
    lens = [math.dist(pos0[a], pos0[b]) for a, b, _ in edges if math.dist(pos0[a], pos0[b]) > 0]
    X = (X - X.mean(axis=0)) / (np.median(lens) if lens else 1.0)
    X0 = X.copy()
    V = -W.copy()
    np.fill_diagonal(V, W.sum(axis=1))
    lam = anchor * float(np.mean(np.diag(V)))
    Vp = np.linalg.inv(V + lam * np.eye(n)) if lam > 0 else np.linalg.pinv(V)
    prev = None
    for _ in range(iters):
        dist = np.linalg.norm(X[:, None, :] - X[None, :, :], axis=2)
        stress = float((W * (dist - D) ** 2).sum() / 2 + lam * ((X - X0) ** 2).sum())
        if prev is not None and abs(prev - stress) < tol * max(prev, 1.0):
            break
        prev = stress
        with np.errstate(divide="ignore", invalid="ignore"):
            B = np.where(dist > 1e-9, -W * D / dist, 0.0)
        np.fill_diagonal(B, 0.0)
        np.fill_diagonal(B, -B.sum(axis=1))
        X = Vp @ (B @ X + lam * X0)
    return X


def fisheye(pos, centre, power=0.5):
    """Radial distortion around the hub: every station keeps its exact bearing from the hub, and its distance
    r becomes r ** power (rescaled), so dense areas near the hub open up and long rural stretches shrink.
    Unlike stress layout it cannot mirror or swing a part of the network to the wrong side."""
    pos = np.array(pos, dtype=float)
    d = pos - np.array(centre, dtype=float)
    r = np.linalg.norm(d, axis=1)
    out = np.zeros_like(d)
    nz = r > 1e-9
    out[nz] = d[nz] / r[nz, None] * (r[nz] ** power)[:, None]
    return out
