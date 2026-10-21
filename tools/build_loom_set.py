"""Add network-diagram (LOOM) maps to a folder of tree maps, so one index page can toggle between the two models.

    python tools/build_loom_set.py --patterns ... --tiplocs ... --network ... --loom-bin DIR --out maps_compare \
        [--workers 7] [--top 2000] [--force] [--index-only]

--out must already hold the tree maps (copy maps/ into it first). Maps are written to OUT/loom/CRS.svg, busiest
stations first; OUT/stations.js gets a "loom" flag per station and OUT/index.html is refreshed from web/.
"""
import argparse
import json
import multiprocessing as mp
import os
import re
import shutil
import signal
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import hub_map as hm  # noqa: E402
import loom_map  # noqa: E402

DATA = NR = BIN = OUT = None
TIMEOUT = 600


def work(crs):
    started = time.time()

    def stop(*_):
        raise TimeoutError("timed out after %ds" % TIMEOUT)

    signal.signal(signal.SIGALRM, stop)
    signal.alarm(TIMEOUT)
    try:
        svg, routes = loom_map.build_svg(DATA, NR, crs, BIN)
        tmp = os.path.join(OUT, "loom", crs + ".svg.tmp")
        with open(tmp, "w") as fh:
            fh.write(svg)
        os.replace(tmp, os.path.join(OUT, "loom", crs + ".svg"))
        return crs, "ok", len(svg), time.time() - started
    except Exception:  # noqa: BLE001 - keep the batch going
        return crs, "error", traceback.format_exc()[-400:], time.time() - started
    finally:
        signal.alarm(0)


def read_stations(out):
    txt = open(os.path.join(out, "stations.js")).read()
    return json.loads(re.search(r"window\.STATIONS\s*=\s*(\[.*\]);", txt, re.S).group(1))


def write_index(out):
    rows = read_stations(out)
    for r in rows:
        r["loom"] = os.path.exists(os.path.join(out, "loom", r["crs"] + ".svg"))
    with open(os.path.join(out, "stations.js"), "w") as fh:
        fh.write("window.STATIONS = " + json.dumps(rows) + ";\n")
    shutil.copy(os.path.join(HERE, "..", "web", "index.html"), os.path.join(out, "index.html"))
    return sum(1 for r in rows if r["loom"]), len(rows)


def main():
    global DATA, NR, BIN, OUT, TIMEOUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--network", required=True)
    ap.add_argument("--loom-bin", required=True, help="folder with the loom, octi and transitmap programs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--top", type=int, default=100000)
    ap.add_argument("--timeout", type=int, default=600, help="seconds before one station is given up on")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--index-only", action="store_true")
    a = ap.parse_args()
    OUT, BIN, TIMEOUT = os.path.abspath(a.out), os.path.abspath(a.loom_bin), a.timeout
    os.makedirs(os.path.join(OUT, "loom"), exist_ok=True)
    if a.index_only:
        print("%d of %d stations have a network map" % write_index(OUT))
        return
    DATA = hm.Data(a.patterns, a.tiplocs)
    NR = hm.load_network(a.network, DATA)
    todo = [r["crs"] for r in sorted(read_stations(OUT), key=lambda r: -r["tph"])][: a.top]
    if not a.force:
        todo = [c for c in todo if not os.path.exists(os.path.join(OUT, "loom", c + ".svg"))]
    print("%d stations to draw, %d workers" % (len(todo), a.workers), flush=True)
    t0, done, failed, size = time.time(), 0, [], 0
    with mp.get_context("fork").Pool(a.workers, maxtasksperchild=25) as pool:
        for crs, status, info, secs in pool.imap_unordered(work, todo):
            done += 1
            if status == "ok":
                size += info
            else:
                failed.append(crs)
                print("[%d/%d] %s %s: %s" % (done, len(todo), crs, status, info), flush=True)
            if done % 25 == 0 or done == len(todo):
                write_index(OUT)
                print("[%d/%d] %.0f min, %.0f MB so far, %d failed" % (done, len(todo), (time.time() - t0) / 60,
                                                                       size / 1e6, len(failed)), flush=True)
    print("finished: %d of %d stations have a network map" % write_index(OUT), "failed:", failed, flush=True)


if __name__ == "__main__":
    main()
