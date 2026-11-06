"""Draw stations with several layout engines and print one comparable score per map, so a change to an engine
can be judged on numbers before it is judged by eye.

    python tools/compare_engines.py --label base                          # CLJ VIC LDS + toy networks, old and blocks
    python tools/compare_engines.py --label fix1 --against base           # the same, with the change from base per map
    python tools/compare_engines.py --stations VIC --engines blocks --no-toy --label quick

Real stations need the data files (default: data/, else ../rail-network-visualiser-data/). Every grouped pattern
is drawn on one page (mode "all"), as the hub_map CLI does with --mode all.

The score, `fair`, is hub_map.hard() with stubs counted and a crossing inside a junction counted as a full crossing.
The tree engine reports no junctions (every crossing it draws is a full crossing), so hard() alone flatters the
blocks engine; `fair` puts both on one scale. `cross` below is crossings + junction_crossings.

Writes maps/compare/<label>/: <station>_<engine>.svg and .png (halo stripped for cairosvg), results.json.
"""
import argparse, json, multiprocessing, os, pathlib, re, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
import hub_map as hm, station_route_map as base  # noqa: E402

TOYS = ("horsham", "don", "vic", "clj", "big")
COLS = ("fair", "hard", "cross", "junction_crossings", "retrace", "stubs", "label_collision", "dot_on_line", "dot_off_route",
        "branch_overlap", "dot_overlap", "leader_lines", "spacing", "routes", "secs")
SHORT = dict(junction_crossings="junc", label_collision="labels", dot_on_line="dotline", dot_off_route="offroute", branch_overlap="overlap",
             dot_overlap="dotdot", leader_lines="leaders")
DATA = NR = None   # set before the pool forks


def fair(rep):
    saved, hm.STUB_METRIC = hm.STUB_METRIC, True
    try:
        return hm.hard(rep) + 0.5 * rep.get("junction_crossings", 0)
    finally:
        hm.STUB_METRIC = saved


def row(rep, spacing, secs):
    r = {k: rep.get(k, 0) for k in COLS if k in rep}
    r.update(fair=round(fair(rep), 2), hard=round(hm.hard(rep), 2),
             cross=rep["crossings"] + rep.get("junction_crossings", 0), spacing=spacing, secs=round(secs))
    return r


def save(out, name, svg):
    (out / (name + ".svg")).write_text(svg)
    try:
        import cairosvg
    except ImportError:
        return
    png = re.sub(r' stroke="#fff" stroke-width="[45]"( stroke-linejoin="round")? paint-order="stroke"', "", svg)
    cairosvg.svg2png(bytestring=png.encode(), write_to=str(out / (name + ".png")), output_width=1800)


def run_real(job):
    crs, eng, out = job
    t = time.time()
    hm.set_engine(hm.engine(eng))
    res = hm.build_map(DATA, NR, crs, verbose=False, mode="all")
    if res is None:
        return crs, eng, None
    svg, routes, sp, rep = res[:4]
    save(pathlib.Path(out), "%s_%s" % (crs, eng), svg)
    return crs, eng, row(rep, sp, time.time() - t)


def run_toy(job):
    net, eng, out = job
    import toy_station
    t = time.time()
    xy, services, hub = toy_station.NETS[net]
    data = toy_station.make(pathlib.Path(tempfile.mkdtemp()), xy, services)
    hm.set_engine(hm.engine(eng))
    hm.SPACINGS, hm.HUB_GAPS, hm.ALL_SPACINGS, hm.ALL_GAPS = [80], [80], [80], [80]   # as toy_station.py
    svg, routes, sp, rep = hm.build_map(data, None, hub, verbose=False, mode="all")[:4]
    save(pathlib.Path(out), "toy-%s_%s" % (net, eng), svg)
    return "toy-" + net, eng, row(rep, sp, time.time() - t)


def table(results, against=None):
    head = "%-12s %-7s" % ("map", "engine") + "".join("%9s" % SHORT.get(c, c) for c in COLS)
    print(head)
    print("-" * len(head))
    for key in sorted(results):
        name, eng = key.split("|")
        r = results[key]
        if r is None:
            print("%-12s %-7s  no routes" % (name, eng))
            continue
        line = "%-12s %-7s" % (name, eng)
        for c in COLS:
            v = r.get(c, "")
            cell = ("%g" % v) if isinstance(v, (int, float)) else str(v)
            if against and key in against and against[key] and c not in ("secs",) and isinstance(v, (int, float)):
                d = v - against[key].get(c, 0)
                if abs(d) > 1e-9:
                    cell += "(%+g)" % round(d, 2)
            line += "%9s" % cell
        print(line)
    if against:
        tot = {e: sum(results[k]["fair"] for k in results if k.endswith("|" + e) and results[k])
               for e in {k.split("|")[1] for k in results}}
        was = {e: sum(against[k]["fair"] for k in results if k.endswith("|" + e) and k in against and against[k])
               for e in tot}
        print("\ntotal fair: " + ", ".join("%s %g (%+g)" % (e, round(tot[e], 2), round(tot[e] - was[e], 2)) for e in sorted(tot)))


def main():
    global DATA, NR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stations", nargs="*", default=["CLJ", "VIC", "LDS"])
    ap.add_argument("--engines", default="old,blocks")
    ap.add_argument("--no-toy", action="store_true", help="skip the invented networks")
    ap.add_argument("--label", required=True, help="output folder under maps/compare/")
    ap.add_argument("--against", help="an earlier label: print the change from it")
    ap.add_argument("--data", help="folder with national_patterns_days.csv, tiplocs.csv, nr_schematic.json")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    a = ap.parse_args()
    engines = a.engines.split(",")
    out = ROOT / "maps" / "compare" / a.label
    out.mkdir(parents=True, exist_ok=True)

    jobs_real, jobs_toy = [], []
    if a.stations:
        d = pathlib.Path(a.data) if a.data else next(
            (p for p in (ROOT / "data", ROOT.parent / "rail-network-visualiser-data")
             if (p / "national_patterns_days.csv").exists()), None)
        if d is None:
            sys.exit("no data folder found: pass --data, or use --stations with no codes for toy networks only")
        DATA = base.Data(str(d / "national_patterns_days.csv"), str(d / "tiplocs.csv"))
        NR = hm.load_network(str(d / "nr_schematic.json"), DATA)
        jobs_real = [(s, e, str(out)) for s in a.stations for e in engines]
    if not a.no_toy:
        jobs_toy = [(n, e, str(out)) for n in TOYS for e in engines]

    ctx = multiprocessing.get_context("fork")   # workers inherit the loaded data
    results = {}
    with ctx.Pool(a.workers) as pool:
        waits = [pool.apply_async(run_real, (j,)) for j in jobs_real] + [pool.apply_async(run_toy, (j,)) for j in jobs_toy]
        for w in waits:
            name, eng, r = w.get()
            results["%s|%s" % (name, eng)] = r
    (out / "results.json").write_text(json.dumps(results, indent=1, sort_keys=True))
    against = None
    if a.against:
        against = json.loads((ROOT / "maps" / "compare" / a.against / "results.json").read_text())
    table(results, against)
    print("\nmaps in", out)


if __name__ == "__main__":
    main()
