"""End-to-end check on a tiny invented network (no real data needed).

A hub with a west arm, an east arm and a north branch; two routes cross the hub hourly in each
direction. The map must come out with no overlapping dots and no crossings.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import hub_map as hm  # noqa: E402
import station_route_map as base  # noqa: E402

# crs: (easting, northing) in metres
STATIONS = {"HUB": (500000, 200000), "W1": (490000, 200000), "W2": (480000, 200000), "W3": (470000, 200000),
            "E1": (510000, 200000), "E2": (520000, 200000), "E3": (530000, 200000),
            "N1": (500000, 210000), "N2": (500000, 220000)}
ROUTES = [["W3", "W2", "W1", "HUB", "E1", "E2", "E3"], ["N2", "N1", "HUB", "E1", "E3"]]


def make_data(tmp):
    pd.DataFrame([{"TiplocCode": c, "CrsCode": c, "StationName": c.title() + " Rail Station",
                   "easting": e, "northing": n} for c, (e, n) in STATIONS.items()]).to_csv(tmp / "t.csv", index=False)
    rows = []
    for day in ("20260908", "20260909", "20260910"):
        for hour in range(9, 16):
            for route in ROUTES:
                for seq in (route, route[::-1]):
                    times = ["%02d:%02d:00" % (hour, 5 * i) for i in range(len(seq))]
                    # make the hub depart inside the same hour as the service starts
                    rows.append({"service_date": day, "rid": "%s%s%d%s" % (day, route[0], hour, seq[0]),
                                 "toc": "XX", "train_id": "1A00", "origin_tpl": seq[0], "destination_tpl": seq[-1],
                                 "pattern": ",".join(seq), "times": ",".join(times)})
    pd.DataFrame(rows).to_csv(tmp / "p.csv", index=False)
    return base.Data(str(tmp / "p.csv"), str(tmp / "t.csv"))


def test_toy_hub_map(tmp_path, monkeypatch):
    data = make_data(tmp_path)
    monkeypatch.setattr(hm, "SPACINGS", [80])
    monkeypatch.setattr(hm, "HUB_GAPS", [80])
    monkeypatch.setattr(hm, "ALL_SPACINGS", [80])
    monkeypatch.setattr(hm, "ALL_GAPS", [80])
    svg, routes, spacing, report, omitted = hm.build_map(data, None, "HUB", verbose=False)
    assert svg.startswith("<svg") and "Hub Routes" in svg
    assert len(routes) == 2 and omitted == 0
    assert report["dot_overlap"] == 0 and report["crossings"] == 0 and report["branch_overlap"] == 0
    assert report["retrace"] == 0
    # the "all" view draws at least as many routes
    svg_all, routes_all, _, _, _ = hm.build_map(data, None, "HUB", verbose=False, mode="all")
    assert len(routes_all) >= len(routes)


def test_loop_is_drawn_not_retraced(tmp_path, monkeypatch):
    """Two ways between B and D (via C, or direct): the route that takes the quieter way must not run out
    along C and back."""
    import pandas as pd
    rows = []
    def add(toc, seq, n):
        for i in range(n):
            rows.append({"service_date": "20260901", "rid": "r%d%s" % (i, seq[0]), "train_id": "x", "toc": toc,
                         "pattern": ",".join(seq), "times": ",".join("10:%02d:00" % (j * 5) for j in range(len(seq)))})
    add("T", ["A", "B", "C", "D", "E"], 12)
    add("T", ["A", "B", "X", "Y", "D", "E"], 6)
    pd.DataFrame(rows).to_csv(tmp_path / "p.csv", index=False)
    names = [{"TiplocCode": c, "CrsCode": c, "StationName": "Stn " + c, "easting": 1000 + 100 * i,
              "northing": 1000 + 20 * (i % 2)} for i, c in enumerate("ABCXYDE")]
    pd.DataFrame(names).to_csv(tmp_path / "t.csv", index=False)
    data = base.Data(str(tmp_path / "p.csv"), str(tmp_path / "t.csv"))
    monkeypatch.setattr(hm, "SPACINGS", [100])
    monkeypatch.setattr(hm, "HUB_GAPS", [80])
    svg, routes, spacing, report, omitted = hm.build_map(data, None, "A", verbose=False)
    assert len(routes) == 2 and report["retrace"] == 0 and report["crossings"] == 0


def test_typical_day_ignores_closure_days():
    """A pattern that ran on only a few of the sampled days (a diversion) is not part of the normal day;
    one that ran on most days is, with the median number of trains per day."""
    rows, sets = [], []
    days = ["202609%02d" % d for d in range(1, 11)]

    def add(pat, times, on):
        stops = tuple(pat.split(","))
        rows.append(("T", pat, stops, tuple(times.split(","))))
        sets.append(set(on))

    add("A,B,C", "10:00:00,10:10:00,10:20:00", days)                  # runs every day
    add("A,X,C", "11:00:00,11:10:00,11:20:00", days[:2])             # diversion on 2 of 10 days
    add("A,D,C", "12:00:00,12:10:00,12:20:00", days[:6])             # runs on 6 of 10 days: normal
    add("E,F", "09:00:00,09:10:00", days)                            # two trains a day
    add("E,F", "09:30:00,09:40:00", days)
    add("G,H", "08:00:00,08:10:00", days[:5])                        # variant 1 of a split service
    add("G,H", "08:01:00,08:11:00", days[5:])                        # the same train retimed: kept once
    out = base.typical_day(rows, sets)
    pats = sorted(",".join(s) for _t, s, _tm in out)
    assert "A,X,C" not in pats
    assert pats.count("A,B,C") == 1 and pats.count("A,D,C") == 1
    assert pats.count("E,F") == 2
    assert pats.count("G,H") == 1
