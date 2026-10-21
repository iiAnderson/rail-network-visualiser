"""Write the LOOM line graph of a station (see src/loom_map.py).

    python tools/export_loom.py CLJ out.json --patterns ... --tiplocs ... --network ...
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import hub_map as hm  # noqa: E402
import loom_map  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hub")
    ap.add_argument("out")
    ap.add_argument("--patterns", required=True)
    ap.add_argument("--tiplocs", required=True)
    ap.add_argument("--network", required=True)
    ap.add_argument("--fisheye", type=float, default=loom_map.FISHEYE)
    a = ap.parse_args()
    data = hm.Data(a.patterns, a.tiplocs)
    g, routes = loom_map.line_graph(data, hm.load_network(a.network, data), a.hub, a.fisheye)
    json.dump(g, open(a.out, "w"))
    print("%s: %d features, %d routes -> %s" % (a.hub, len(g["features"]), len(routes), a.out))


if __name__ == "__main__":
    main()
