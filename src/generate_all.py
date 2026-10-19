"""Generate route maps for many stations in parallel. Resumable: stations that already have a
map are skipped, so you can stop it and run it again (or raise --top later to add more).

    python3 generate_all.py --patterns ../data/national_patterns.csv --tiplocs ../data/tiplocs.csv \
        --network ../data/nr_schematic.json --top 300 --workers 6

Stations are ranked by how many trains an hour call there (busiest first) and the busiest --top
that have at least --min-tph are drawn. Besides the SVGs, the output folder gets index.html (a
station selector), stations.js (its station list) and manifest.json (per-map results).
"""
import argparse
import collections
import json
import multiprocessing as mp
import os
import shutil
import time
import traceback

import hub_map as hm
import station_route_map as base

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = NR = None  # set before the pool is created so forked workers inherit them
OUT = None


def rank_stations(data):
    """Trains per hour (each way) calling at each station inside the daytime window."""
    lo, hi = base.WINDOW
    slots = data.days * (hi - lo)
    calls = collections.Counter()
    for _toc, stops, times in data.services:
        for c, t in zip(stops, times):
            if lo <= int(t[:2]) < hi:
                calls[c] += 1
    return {c: n / slots / 2 for c, n in calls.items()}


def work(crs):
    started = time.time()
    try:
        res = hm.build_map(DATA, NR, crs, verbose=False)
        if res is None:
            return crs, "no routes", None, time.time() - started
        svg, routes, spacing, report = res
        with open(os.path.join(OUT, crs + ".svg"), "w") as fh:
            fh.write(svg)
        report = {k: v for k, v in report.items() if k != "where"}
        report.update(spacing=spacing, routes=len(routes))
        return crs, "ok", report, time.time() - started
    except Exception:  # keep the batch going; the traceback is logged
        return crs, "error", traceback.format_exc(), time.time() - started


def write_selector(manifest, tph):
    rows = []
    for crs, m in manifest.items():
        rows.append({"crs": crs, "name": m["name"], "tph": round(tph.get(crs, 0), 1),
                     "routes": m.get("routes"), "spacing": m.get("spacing")})
    rows.sort(key=lambda r: -r["tph"])
    with open(os.path.join(OUT, "stations.js"), "w") as fh:
        fh.write("window.STATIONS = " + json.dumps(rows) + ";\n")
    with open(os.path.join(OUT, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)


def main():
    global DATA, NR, OUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--network", default=None, help="nr_schematic.json (optional)")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "maps"))
    ap.add_argument("--top", type=int, default=300, help="how many of the busiest stations to draw")
    ap.add_argument("--min-tph", type=float, default=2.0, help="ignore stations with fewer calling trains/hour")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--stations", nargs="*", help="draw exactly these CRS codes instead of ranking")
    ap.add_argument("--timeout", type=float, default=20, help="minutes before one station is given up on")
    ap.add_argument("--force", action="store_true", help="redraw maps that already exist")
    a = ap.parse_args()

    OUT = os.path.abspath(a.out)
    os.makedirs(OUT, exist_ok=True)
    shutil.copy(os.path.join(HERE, "..", "web", "index.html"), os.path.join(OUT, "index.html"))
    DATA = base.Data(a.patterns, a.tiplocs)
    NR = hm.load_network(a.network, DATA)

    tph = rank_stations(DATA)
    if a.stations:
        todo = list(a.stations)
    else:
        ranked = sorted(tph, key=lambda c: -tph[c])
        todo = [c for c in ranked if tph[c] >= a.min_tph and c in DATA.names][: a.top]
    manifest_path = os.path.join(OUT, "manifest.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    if not a.force:
        todo = [c for c in todo if not os.path.exists(os.path.join(OUT, c + ".svg"))]
    print("%d stations to draw (%d already done), %d workers" % (len(todo), len(manifest), a.workers), flush=True)
    write_selector(manifest, tph)

    t0, done, failed = time.time(), 0, []
    ctx = mp.get_context("fork")
    queue = ctx.Queue()

    def child(crs):
        queue.put(work(crs))

    pending, running = list(todo), {}   # crs -> (process, started)
    while pending or running:
        while pending and len(running) < a.workers:
            crs = pending.pop(0)
            p = ctx.Process(target=child, args=(crs,), daemon=True)
            p.start()
            running[crs] = (p, time.time())
        try:
            crs, status, info, secs = queue.get(timeout=5)
            running.pop(crs)[0].join()
        except Exception:  # nothing finished yet: look for crashed or runaway workers
            for crs, (p, started) in list(running.items()):
                if not p.is_alive() and queue.empty():
                    status, info = "crashed (exit %s)" % p.exitcode, None
                elif time.time() - started > a.timeout * 60:
                    p.kill()
                    status, info = "timed out", None
                else:
                    continue
                running.pop(crs)
                done += 1
                failed.append((crs, status))
                print("[%d/%d] %s %s" % (done, len(todo), crs, status), flush=True)
            continue
        done += 1
        if status == "ok":
            manifest[crs] = dict(info, name=DATA.names.get(crs, crs))
            write_selector(manifest, tph)
            print("[%d/%d] %s %-28s %5.0fs  spacing %s  %s" % (
                done, len(todo), crs, DATA.names.get(crs, crs)[:28], secs, info["spacing"],
                {k: v for k, v in info.items() if k in ("label_collision", "crossings", "dot_on_line", "branch_overlap")}),
                flush=True)
        else:
            failed.append((crs, status))
            print("[%d/%d] %s %s%s" % (done, len(todo), crs, status, "" if status == "no routes" else "\n" + str(info)),
                  flush=True)
    print("finished %d maps in %.0f min; %d skipped/failed: %s" % (
        done - len(failed), (time.time() - t0) / 60, len(failed), failed[:20]), flush=True)


if __name__ == "__main__":
    main()
