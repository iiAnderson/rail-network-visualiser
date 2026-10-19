"""Step 1 of the national map: put every service on the PDF graph and count tph per segment.

    python src/national_edges.py --patterns data/national_patterns.csv --tiplocs data/tiplocs.csv \
        --network data/nr_schematic.json --out data/national_edges.pkl

Segment tph = services that traverse the segment (call or pass) in the window, per hour, each way.
Station tph = calls in the window, per hour, each way (as generate_all.rank_stations).
"""
import argparse
import collections
import heapq
import pickle
import sys
import time

import nr_route_map as nrm
from station_route_map import Data, WINDOW


# TIPLOCs / stations whose name in tiplocs.csv does not match the PDF label
ALIASES = {"WCHAPXR": "Whitechapel", "PADTLL": "Paddington", "BONDST": "Bond Street",
           "LIVSTLL": "London Liverpool Street", "TOTCTRD": "Tottenham Court Road",
           "FRNDXR": "Farringdon", "CAMBSTH": "Cambridge South", "WOLWXR": "Woolwich",
           "CUSTMHS": "Custom House", "CANWHRF": "Canary Wharf", "BRENTX": "Brent Cross West",
           "SHF": "Sheffield", "LES": "Leigh", "WIJ": "Willesden Junction", "QPW": "Queens Park",
           "GPO": "Gospel Oak", "KNL": "Kensal Green", "KPA": "Kensington Olympia",
           "HST": "High Street", "AGS": "Argyle St", "WHD": "West Hampstead", "WHP": "West Hampstead",
           "TPB": "Thorpe", "LVJ": "James Street", "BKQ": "Hamilton Square", "LVL": "Liverpool Lime Street",
           "GQL": "Glasgow Queen Street", "GCL": "Glasgow Central", "LPY": "Liverpool South Parkway"}


UNCONFIRMED = 4.0   # cost multiplier on a PDF line no train is seen to use between adjacent stops


def confirmed_edges(data, node_of, nr):
    """PDF edges that join two consecutive calling points of some service. The PDF also draws the
    Underground and other non-National Rail lines; a path should only use those as a last resort."""
    seen = set()
    for _, stops, _ in data.services:
        for a, b in zip(stops, stops[1:]):
            if a in node_of and b in node_of:
                u, v = node_of[a], node_of[b]
                if v in nr.adj.get(u, ()):
                    seen.add((u, v) if u < v else (v, u))
    return seen


def weighted_path(nr, ok, a, b):
    dist, prev, pq = {a: 0.0}, {}, [(0.0, a)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == b:
            break
        if d > dist[u]:
            continue
        for v, w in nr.adj[u].items():
            nd = d + w * (1.0 if ((u, v) if u < v else (v, u)) in ok else UNCONFIRMED) + 0.5
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u
                heapq.heappush(pq, (nd, v))
    if b not in dist:
        return None
    out = [b]
    while out[-1] != a:
        out.append(prev[out[-1]])
    return out[::-1]


def in_window(t):
    return WINDOW[0] <= int(t[:2]) < WINDOW[1]


def build_edges(data, nr):
    """Return dict with per-edge, per-operator and per-station counts (raw counts, not tph)."""
    slots = data.days * (WINDOW[1] - WINDOW[0])
    scale = 1.0 / slots / 2
    node_of = {}
    allc = {c for _, stops, _ in data.services for c in stops}
    for c in sorted(allc):
        n = nr.match(c, data.names.get(c, c), data)
        if n is None and c in ALIASES:
            n = nr.match(c, ALIASES[c], data)
        if n is not None:
            node_of[c] = n
    # last resort: the nearest PDF station nobody else claimed, if it is where the station should be
    taken = set(node_of.values())
    free = [n for n in nr.node.values() if n["station"] and n["id"] not in taken]
    for c in sorted(allc - set(node_of)):
        if c in data.xy and free:
            px, py = nr.predict(data.xy[c][0], -data.xy[c][1])
            n = min(free, key=lambda n: (n["x"] - px) ** 2 + (n["y"] - py) ** 2)
            if (n["x"] - px) ** 2 + (n["y"] - py) ** 2 < 25 ** 2:
                node_of[c] = n["id"]
                free.remove(n)
    crs_of = {}
    for c, n in node_of.items():
        crs_of.setdefault(n, c)
    ok = confirmed_edges(data, node_of, nr)
    hop = {}
    edge_toc = collections.defaultdict(collections.Counter)  # (u, v) u<v -> toc -> count
    calls = collections.Counter()                           # crs -> calls in window
    unmatched = collections.Counter()
    failed = collections.Counter()
    for toc, stops, times in data.services:
        for c, t in zip(stops, times):
            if in_window(t):
                calls[c] += 1
        for i in range(len(stops) - 1):
            a, b = stops[i], stops[i + 1]
            if not in_window(times[i]):
                continue
            if a not in node_of or b not in node_of:
                unmatched[a if a not in node_of else b] += 1
                continue
            k = (node_of[a], node_of[b])
            if k not in hop:
                hop[k] = weighted_path(nr, ok, *k) if k[0] != k[1] else None
            p = hop[k]
            if not p:
                failed[(a, b)] += 1
                continue
            for u, v in zip(p, p[1:]):
                edge_toc[(u, v) if u < v else (v, u)][toc] += 1
    return {"scale": scale, "node_of": node_of, "crs_of": crs_of, "edge_toc": dict(edge_toc),
            "confirmed": ok, "calls": calls, "unmatched": unmatched, "failed": failed}


def edge_tph(res):
    return {e: sum(c.values()) * res["scale"] for e, c in res["edge_toc"].items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--network", required=True)
    ap.add_argument("--out", default="data/national_edges.pkl")
    a = ap.parse_args()
    t0 = time.time()
    data = Data(a.patterns, a.tiplocs)
    nr = nrm.Network(a.network)
    nr.build_anchors(data)
    res = build_edges(data, nr)
    pickle.dump(res, open(a.out, "wb"))
    tph = edge_tph(res)
    name = lambda n: data.names.get(res["crs_of"].get(n), nr.node[n]["name"] or n)
    print("%d services, %d stations matched to the PDF of %d; %d edges carry traffic (%.0fs)" % (
        len(data.services), len(res["node_of"]), len({c for _, s, _ in data.services for c in s}),
        len(tph), time.time() - t0))
    print("unmatched hops: %d (stations: %s)" % (sum(res["unmatched"].values()),
          ", ".join("%s %d" % (data.names.get(c, c), n) for c, n in res["unmatched"].most_common(10))))
    print("no PDF path: %d hops (%s)" % (sum(res["failed"].values()), ", ".join(
          "%s-%s %d" % (data.names.get(x, x), data.names.get(y, y), n) for (x, y), n in res["failed"].most_common(8))))
    print("busiest segments (tph each way):")
    for e, v in sorted(tph.items(), key=lambda kv: -kv[1])[:20]:
        print("  %5.1f  %s - %s" % (v, name(e[0]), name(e[1])))


if __name__ == "__main__":
    sys.exit(main())
