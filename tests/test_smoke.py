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
    svg, routes, spacing, report = hm.build_map(data, None, "HUB", verbose=False)
    assert svg.startswith("<svg") and "Hub Routes" in svg
    assert len(routes) == 2
    assert report["dot_overlap"] == 0 and report["crossings"] == 0 and report["branch_overlap"] == 0
