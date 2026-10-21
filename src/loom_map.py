"""Network-diagram model: lay a station's routes out as a graph (not a tree) with LOOM.

    routes (after evidence-first expansion) -> line graph -> fisheye around the hub (true bearings, compressed
    distance) -> LOOM: line ordering + octilinear layout -> LOOM's SVG renderer -> tidy.

LOOM (https://github.com/ad-freiburg/loom, GPL-3.0) is run as separate programs; see tools/build_loom_set.py.
"""
import collections
import copy
import json
import math
import os
import re
import subprocess

import numpy as np

import hub_map as hm
import schematic
import station_route_map as base

FISHEYE = 0.5            # radial compression around the hub: bearings kept, distance ** FISHEYE
OCTI_ARGS = ["--geo-pen", "4"]   # keep the drawing close to the geography (stops a long edge routing round the map)
RENDER_ARGS = ["-l", "--line-width", "160", "--line-spacing", "80", "--station-label-textsize", "520",
               "--line-label-textsize", "320", "--outline-width", "12"]


def lonlat(x, y):
    """Stations carry OSGB metres with the northing negated (SVG y down); a local flat approximation is enough
    for LOOM, which only needs positions that keep the layout of the network."""
    lat0 = 54.0
    return (-2.0 + (x - 400000.0) / (111320.0 * math.cos(math.radians(lat0))), lat0 + (-y - 300000.0) / 111320.0)


def line_graph(data, nr, hub, fisheye=FISHEYE):
    """GeoJSON line graph for LOOM: stations are nodes, consecutive stops are edges, routes are the lines."""
    base.K_STOPS, base.MAX_ROUTES, base.MIN_TPH = 999, 40, hm.ALL_MIN_TPH
    routes = copy.deepcopy(base.discover_routes(data, hub))
    hm.letters(routes)
    saved, hm.PDF_FILTER = hm.PDF_FILTER, True
    try:
        hm.expand_via_pdf(routes, nr, data)
    finally:
        hm.PDF_FILTER = saved
    stations = {c for r in routes for c in r["path"]}
    pos = {c: data.xy[c] for c in stations if c in data.xy}
    adj = collections.defaultdict(set)
    for r in routes:
        for a, b in zip(r["path"], r["path"][1:]):
            if a != b:
                adj[a].add(b)
                adj[b].add(a)
    for _ in range(50):     # stations without coordinates sit at the mean of their placed neighbours
        grew = False
        for c in stations - set(pos):
            ps = [pos[n] for n in adj[c] if n in pos]
            if ps:
                pos[c] = (sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps))
                grew = True
        if not grew:
            break
    edges = collections.defaultdict(list)
    for r in routes:
        for a, b in zip(r["path"], r["path"][1:]):
            if a != b and a in pos and b in pos:
                key = tuple(sorted((a, b)))
                if r["id"] not in edges[key]:
                    edges[key].append(r["id"])
    names = sorted({c for e in edges for c in e})
    if fisheye and names:
        F = schematic.fisheye(np.array([pos[c] for c in names]), pos.get(hub, (0.0, 0.0)), fisheye)
        ix = {c: i for i, c in enumerate(names)}
        lens = [float(np.linalg.norm(F[ix[a]] - F[ix[b]])) for a, b in edges]
        k = 3000.0 / max(float(np.median([x for x in lens if x > 0] or [1.0])), 1e-9)   # ~3 km per hop
        pos = {c: (float(F[ix[c], 0]) * k + 400000.0, float(F[ix[c], 1]) * k - 300000.0) for c in names}
    colour = {r["id"]: base.PALETTE[i % len(base.PALETTE)].lstrip("#") for i, r in enumerate(routes)}
    feats = []
    for c in names:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(lonlat(*pos[c]))},
                      "properties": {"id": c, "station_id": c, "station_label": data.names.get(c, c)}})
    for (a, b), ids in sorted(edges.items()):
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString", "coordinates": [list(lonlat(*pos[a])), list(lonlat(*pos[b]))]},
                      "properties": {"from": a, "to": b,
                                     "lines": [{"id": i, "label": i, "color": colour[i]} for i in ids]}})
    return {"type": "FeatureCollection", "features": feats}, routes


def run_pipeline(graph_json, bin_dir, timeout=600):
    """loom | octi | transitmap on a GeoJSON string; returns the SVG text."""
    data = graph_json if isinstance(graph_json, bytes) else (graph_json if isinstance(graph_json, str) else json.dumps(graph_json)).encode()
    for cmd in (["loom"], ["octi"] + OCTI_ARGS, ["transitmap"] + RENDER_ARGS):
        p = subprocess.run([os.path.join(bin_dir, cmd[0])] + cmd[1:], input=data, capture_output=True, timeout=timeout)
        if p.returncode != 0 or not p.stdout:
            raise RuntimeError("%s failed (%s): %s" % (cmd[0], p.returncode, p.stderr.decode()[-300:]))
        data = p.stdout
    return data.decode()


def layout_json(graph_json, bin_dir, timeout=600):
    """loom | octi on a GeoJSON line graph; returns LOOM's octilinear layout as a dict."""
    data = graph_json if isinstance(graph_json, bytes) else (graph_json if isinstance(graph_json, str) else json.dumps(graph_json)).encode()
    for cmd in (["loom"], ["octi"] + OCTI_ARGS):
        p = subprocess.run([os.path.join(bin_dir, cmd[0])] + cmd[1:], input=data, capture_output=True, timeout=timeout)
        if p.returncode != 0 or not p.stdout:
            raise RuntimeError("%s failed (%s): %s" % (cmd[0], p.returncode, p.stderr.decode()[-300:]))
        data = p.stdout
    return json.loads(data)


def tidy_svg(svg):
    """Scale to the window (LOOM writes a pixel size of tens of thousands), white background."""
    svg = re.sub(r"<\?xml[^>]*\?>\s*|<!DOCTYPE[^>]*>\s*", "", svg)
    head = re.search(r"<svg[^>]*>", svg).group(0)
    vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', head).group(1).split()]
    new = re.sub(r'\s(width|height)="[^"]*"', "", head)
    new = new.replace("<svg", '<svg font-family="Helvetica Neue, Helvetica, Arial, sans-serif"', 1)
    bg = '<rect x="%g" y="%g" width="%g" height="%g" fill="#fff"/>' % (vb[0], vb[1], vb[2], vb[3])
    return svg.replace(head, new + bg, 1)


def build_svg(data, nr, hub, bin_dir):
    graph, routes = line_graph(data, nr, hub)
    return tidy_svg(run_pipeline(graph, bin_dir)), routes


def build_svg_own(data, nr, hub, bin_dir):
    """The network layout drawn by our own renderer (src/network_render.py)."""
    import network_render
    graph, routes = line_graph(data, nr, hub)
    svg, report = network_render.render(data, hub, routes, layout_json(graph, bin_dir))
    return svg, report
