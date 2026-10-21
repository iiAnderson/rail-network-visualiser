"""Step 2 of plans/BETTER_LOOPS.md: how many stations whose graph has loops reduce to series-parallel?

    python tools/measure_sp.py --patterns .../national_patterns_days.csv --tiplocs .../tiplocs.csv \
        --network .../nr_schematic.json [--top 1307] [--workers 7] [--out sp.json]

Per station (every grouped pattern, as in the "all" view, and the clean pool: the busiest <= 24
patterns of at least 0.3 tph) it builds the station graph the layout sees and reports its blocks.
"""
import argparse
import json
import multiprocessing as mp
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import generate_all as ga  # noqa: E402
import hub_map as hm  # noqa: E402
import loops  # noqa: E402
import station_route_map as base  # noqa: E402


def graph_edges(data, nr, hub, pool_only):
    base.K_STOPS = 999
    base.MAX_ROUTES = 40
    base.MIN_TPH = hm.ALL_MIN_TPH
    every = base.discover_routes(data, hub)
    routes = [r for r in every if r["tph"] >= hm.CLEAN_MIN_TPH][:hm.MAX_ROUTES] if pool_only else every
    if not routes:
        return None
    hm.letters(routes)
    hm.expand_via_pdf(routes, nr, data)
    return {tuple(sorted(e)) for r in routes for e in zip(r["path"], r["path"][1:]) if e[0] != e[1]}


def measure(crs):
    out = {}
    for name, pool_only in (("all", False), ("pool", True)):
        try:
            edges = graph_edges(ga.DATA, ga.NR, crs, pool_only)
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": repr(e)}
            continue
        if edges is None:
            out[name] = None
            continue
        bl = loops.blocks(edges)
        res = [loops.reduce_block(b) for b in bl]
        out[name] = {"blocks": len(bl), "reducible": sum(1 for ok, _ in res if ok),
                     "core": [n for ok, n in res if not ok], "sizes": [len(b) for b in bl]}
    return crs, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--network", required=True)
    ap.add_argument("--top", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--out", default="sp.json")
    a = ap.parse_args()
    ga.DATA = hm.Data(a.patterns, a.tiplocs)
    ga.NR = hm.load_network(a.network, ga.DATA)
    tph = ga.rank_stations(ga.DATA)
    stations = [c for c, t in sorted(tph.items(), key=lambda kv: -kv[1])
                if t >= 1.0 and c in ga.DATA.names][:a.top]
    with mp.get_context("fork").Pool(a.workers) as pool:
        res = dict(pool.imap_unordered(measure, stations, chunksize=4))
    json.dump(res, open(a.out, "w"))
    for view in ("all", "pool"):
        have = [r[view] for r in res.values() if r.get(view) and "error" not in r[view]]
        loopy = [r for r in have if r["blocks"]]
        full = [r for r in loopy if r["reducible"] == r["blocks"]]
        nb = sum(r["blocks"] for r in loopy)
        nr_ = sum(r["reducible"] for r in loopy)
        print("%-4s stations %d, with loops %d, fully reducible %d (%.0f%%); blocks %d, reducible %d (%.0f%%)" % (
            view, len(have), len(loopy), len(full), 100 * len(full) / max(1, len(loopy)), nb, nr_,
            100 * nr_ / max(1, nb)))
    for c in ("CLJ", "HRH", "CRW", "EUS", "MAN", "SHF", "BGN", "DON", "RDG"):
        print(c, res.get(c))


if __name__ == "__main__":
    main()
