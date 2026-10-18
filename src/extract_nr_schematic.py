"""Extract the station/line network from the National Rail All Stations route map PDF.

Needs PyMuPDF (not a project dependency, so run it from a throwaway venv):

    python3 -m venv /tmp/nrvenv && /tmp/nrvenv/bin/pip install pymupdf
    /tmp/nrvenv/bin/python -I extract_nr_schematic.py MAP.pdf ../data/nr_schematic.json

Output: stations (white dots with their label), and edges between consecutive nodes along
each coloured service line (nodes are stations plus junctions where a line ends on another).
Edge geometry is the PDF's own octilinear polyline, so it can be reused as a schematic base.
"""
import collections
import json
import math
import sys

import pymupdf

LINE_MIN_W = 0.5
SNAP = 1.6            # endpoint -> other line (junction) tolerance, pt
MERGE = 2.0           # dots closer than this (per axis) are one station
DOT_ON_LINE = 2.8     # station dot -> line tolerance, pt
LABEL_MAX = 9.0       # label -> dot distance, pt
IGNORE_COLOURS = {(1.0, 1.0, 1.0)}
GREY = (0.82, 0.83, 0.83)


def flatten(items):
    pts = []
    for it in items:
        k = it[0]
        if k == "l":
            seg = [it[1], it[2]]
        elif k == "c":
            p0, p1, p2, p3 = it[1:5]
            seg = [(
                (1 - t) ** 3 * p0.x + 3 * (1 - t) ** 2 * t * p1.x + 3 * (1 - t) * t * t * p2.x + t ** 3 * p3.x,
                (1 - t) ** 3 * p0.y + 3 * (1 - t) ** 2 * t * p1.y + 3 * (1 - t) * t * t * p2.y + t ** 3 * p3.y)
                for t in [i / 8 for i in range(9)]]
            seg = [pymupdf.Point(x, y) for x, y in seg]
        else:
            continue
        for p in seg:
            q = (round(p.x, 3), round(p.y, 3))
            if not pts or pts[-1] != q:
                pts.append(q)
    return pts


def proj(pt, a, b):
    """(distance, t along ab) from pt to segment ab."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((pt[0] - ax) * dx + (pt[1] - ay) * dy) / L2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(pt[0] - px, pt[1] - py), t


def main(pdf, out):
    page = pymupdf.open(pdf)[0]
    drawings = page.get_drawings()

    lines, dots = [], []
    for d in drawings:
        col = tuple(round(v, 2) for v in d["color"]) if d["color"] else None
        if d["type"] == "s" and col and col not in IGNORE_COLOURS and col != GREY \
                and (d["width"] or 0) >= LINE_MIN_W:
            pts = flatten(d["items"])
            if len(pts) >= 2:
                lines.append({"col": col, "pts": pts, "w": round(d["width"], 2)})
        elif d["type"] == "fs" and d["fill"] == (1.0, 1.0, 1.0) and d["color"] == (0.0, 0.0, 0.0):
            r = d["rect"]
            if max(r.width, r.height) <= 14:
                dots.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, max(r.width, r.height)))
    # parallel operator lines each get their own dot ~1.4pt apart: merge them into one station
    merged = []
    for x, y, sz in sorted(dots):
        for m in merged:
            if abs(m[0] - x) <= MERGE and abs(m[1] - y) <= MERGE:
                n = m[3]
                m[0], m[1] = (m[0] * n + x) / (n + 1), (m[1] * n + y) / (n + 1)
                m[2] = max(m[2], sz)
                m[3] = n + 1
                break
        else:
            merged.append([x, y, sz, 1])
    dots = [(m[0], m[1], m[2]) for m in merged]
    print("lines", len(lines), "dots", len(dots), file=sys.stderr)

    # ---- label per dot: text lines nearest to a dot, grouped per dot
    tl = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if txt:
                tl.append((txt, ln["bbox"]))
    cell = 12.0
    grid = collections.defaultdict(list)
    for i, (x, y, _) in enumerate(dots):
        grid[(int(x // cell), int(y // cell))].append(i)
    per_dot = collections.defaultdict(list)
    for txt, (x0, y0, x1, y1) in tl:
        best, bd = None, LABEL_MAX
        cx0, cy0 = int((x0 - LABEL_MAX) // cell), int((y0 - LABEL_MAX) // cell)
        cx1, cy1 = int((x1 + LABEL_MAX) // cell), int((y1 + LABEL_MAX) // cell)
        for gx in range(cx0, cx1 + 1):
            for gy in range(cy0, cy1 + 1):
                for i in grid.get((gx, gy), []):
                    x, y, _ = dots[i]
                    dx = max(x0 - x, 0, x - x1)
                    dy = max(y0 - y, 0, y - y1)
                    dd = math.hypot(dx, dy)
                    if dd < bd:
                        best, bd = i, dd
        if best is not None:
            per_dot[best].append((y0, x0, txt))

    # ---- graph nodes: dots (stations) first
    nodes = []   # (x, y, name or None)
    for i, (x, y, size) in enumerate(dots):
        name = None
        if i in per_dot:
            parts = [t for _, _, t in sorted(per_dot[i])]
            name = " ".join(parts)
        nodes.append([x, y, name])
    sizes = [d[2] for d in dots]

    # ---- per line: ordered list of nodes (stations on it + junctions)
    lgrid = collections.defaultdict(list)
    for li, ln in enumerate(lines):
        for si in range(len(ln["pts"]) - 1):
            a, b = ln["pts"][si], ln["pts"][si + 1]
            for gx in range(int(min(a[0], b[0]) // cell) - 1, int(max(a[0], b[0]) // cell) + 2):
                for gy in range(int(min(a[1], b[1]) // cell) - 1, int(max(a[1], b[1]) // cell) + 2):
                    lgrid[(gx, gy)].append((li, si))

    cum = []
    for ln in lines:
        c = [0.0]
        for a, b in zip(ln["pts"], ln["pts"][1:]):
            c.append(c[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        cum.append(c)

    on_line = collections.defaultdict(dict)  # line -> node -> arclength
    def attach(node, li, tol):
        x, y = nodes[node][0], nodes[node][1]
        ln = lines[li]
        best = None
        for si in range(len(ln["pts"]) - 1):
            d, t = proj((x, y), ln["pts"][si], ln["pts"][si + 1])
            if d <= tol and (best is None or d < best[0]):
                best = (d, cum[li][si] + t * (cum[li][si + 1] - cum[li][si]))
        if best:
            on_line[li][node] = best[1]

    for ni in range(len(nodes)):
        x, y = nodes[ni][0], nodes[ni][1]
        seen = set()
        for gx in (int(x // cell) - 1, int(x // cell), int(x // cell) + 1):
            for gy in (int(y // cell) - 1, int(y // cell), int(y // cell) + 1):
                for li, _ in lgrid.get((gx, gy), []):
                    if li not in seen:
                        seen.add(li)
                        # interchange capsules are long: a line can pass through one well off-centre
                        attach(ni, li, DOT_ON_LINE + (0.45 * sizes[ni] if sizes[ni] > 2.5 else 0.0))

    # junctions: a line end that touches another line (or another line's end)
    n_station = len(nodes)
    junction_at = {}
    def junction(x, y):
        key = (round(x / SNAP), round(y / SNAP))
        for kx in (key[0] - 1, key[0], key[0] + 1):
            for ky in (key[1] - 1, key[1], key[1] + 1):
                j = junction_at.get((kx, ky))
                if j is not None and math.hypot(nodes[j][0] - x, nodes[j][1] - y) <= SNAP:
                    return j
        nodes.append([x, y, None])
        junction_at[key] = len(nodes) - 1
        return len(nodes) - 1

    for li, ln in enumerate(lines):
        for end in (ln["pts"][0], ln["pts"][-1]):
            # snap to a station node already on this line end?
            near_station = None
            for ni in on_line[li]:
                if ni < n_station and math.hypot(nodes[ni][0] - end[0], nodes[ni][1] - end[1]) <= DOT_ON_LINE + (0.45 * sizes[ni] if sizes[ni] > 2.5 else 0.0):
                    near_station = ni
            if near_station is not None:
                continue
            others = set()
            gx0, gy0 = int(end[0] // cell), int(end[1] // cell)
            for gx in (gx0 - 1, gx0, gx0 + 1):
                for gy in (gy0 - 1, gy0, gy0 + 1):
                    for lj, sj in lgrid.get((gx, gy), []):
                        if lj != li:
                            others.add(lj)
            touching = []
            for lj in others:
                pj = lines[lj]["pts"]
                best = None
                for sj in range(len(pj) - 1):
                    d, t = proj(end, pj[sj], pj[sj + 1])
                    if d <= SNAP and (best is None or d < best[0]):
                        best = (d, cum[lj][sj] + t * (cum[lj][sj + 1] - cum[lj][sj]))
                if best:
                    touching.append((lj, best[1]))
            if not touching:
                continue
            j = junction(end[0], end[1])
            on_line[li][j] = 0.0 if end == ln["pts"][0] else cum[li][-1]
            for lj, s in touching:
                on_line[lj][j] = s

    # ---- edges between consecutive nodes along each line
    edges = {}
    for li, onl in on_line.items():
        order = sorted(onl.items(), key=lambda kv: kv[1])
        ln, c = lines[li], cum[li]
        for (a, sa), (b, sb) in zip(order, order[1:]):
            if a == b or sb - sa < 0.05:
                continue
            key = (min(a, b), max(a, b))
            geom = subline(ln["pts"], c, sa, sb)
            if a > b:
                geom = geom[::-1]
            e = edges.setdefault(key, {"a": key[0], "b": key[1], "pts": geom, "cols": set()})
            e["cols"].add(ln["col"])
            if len(e["pts"]) < len(geom) and abs(len(geom) - len(e["pts"])) < 0:
                e["pts"] = geom
    outd = {
        "nodes": [{"id": i, "x": round(n[0], 2), "y": round(n[1], 2), "name": n[2],
                   "station": i < n_station} for i, n in enumerate(nodes)],
        "edges": [{"a": e["a"], "b": e["b"], "pts": [[round(x, 2), round(y, 2)] for x, y in e["pts"]],
                   "cols": sorted(e["cols"])} for e in edges.values()],
    }
    json.dump(outd, open(out, "w"))
    named = sum(1 for n in nodes[:n_station] if n[2])
    print("nodes", len(nodes), "stations", n_station, "named", named, "edges", len(edges), file=sys.stderr)


def subline(pts, cum, s0, s1):
    out = []
    for i in range(len(pts) - 1):
        a, b = cum[i], cum[i + 1]
        if b < s0 or a > s1:
            continue
        L = b - a
        def at(s):
            t = 0 if L == 0 else (s - a) / L
            return (pts[i][0] + t * (pts[i + 1][0] - pts[i][0]), pts[i][1] + t * (pts[i + 1][1] - pts[i][1]))
        p0 = at(max(s0, a))
        p1 = at(min(s1, b))
        if not out or out[-1] != p0:
            out.append(p0)
        out.append(p1)
    return out


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
