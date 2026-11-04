"""Skeleton extraction and block layout on toy networks (plans/BLOCK_LAYOUT.md)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import skeleton  # noqa: E402


def routes_of(*paths, tph=1.0):
    return [{"id": chr(65 + i), "path": list(p), "calls": set(p), "tph": tph, "toc": "T"} for i, p in enumerate(paths)]


# A Horsham-shaped network: the hub, a split station S, two arms to M (one with a leaf off it), and a line
# beyond M.  stations: hub-s ; s-a1-a2-m ; s-b1-b2-b3-m ; b2-leaf ; m-n1-n2
XY = {"hub": (0, 0), "s": (1000, 0), "a1": (2000, 300), "a2": (3000, 300), "m": (4000, 0),
      "b1": (2000, -400), "b2": (3000, -400), "b3": (3500, -300), "leaf": (3000, -900), "n1": (5000, 0), "n2": (6000, 0)}
HORSHAM = routes_of(["hub", "s", "a1", "a2", "m", "n1", "n2"],
                    ["hub", "s", "b1", "b2", "b3", "m", "n1", "n2"],
                    ["hub", "s", "b1", "b2", "leaf"])


def test_skeleton_corridors_and_blocks():
    sk = skeleton.Skeleton(HORSHAM, "hub", XY)
    assert sk.nodes == {"hub", "s", "m", "b2", "leaf", "n2"}   # leaves end a corridor too
    by_ends = {(c["a"], c["b"]): c for c in sk.corridors}
    assert set(by_ends) == {("hub", "s"), ("s", "m"), ("s", "b2"), ("b2", "m"), ("b2", "leaf"), ("m", "n2")}
    assert by_ends[("s", "m")]["stations"] == ["s", "a1", "a2", "m"]
    assert by_ends[("s", "b2")]["block"] is not None and by_ends[("hub", "s")]["block"] is None
    assert by_ends[("b2", "leaf")]["block"] is None and by_ends[("m", "n2")]["block"] is None
    assert by_ends[("s", "m")]["routes"] == {"A"} and by_ends[("s", "b2")]["routes"] == {"B", "C"}
    assert len(sk.blocks) == 1 and sk.entry(0) == "s"
    # bearings: the corridor to a1 leaves s pointing east and a little south (SVG y down: +y)
    b = sk.bearing[(by_ends[("s", "m")]["id"], 0)]
    assert abs(b) < 0.5


def test_balloon_loop_and_parallel_corridors():
    # a loop that returns to the junction it left, and two corridors between the same two junctions
    xy = {"h": (0, 0), "j": (1000, 0), "k1": (1500, 400), "k2": (2000, 0), "k3": (1500, -400),
          "p": (3000, 0), "q1": (2500, 300), "q2": (2500, -300)}
    rs = routes_of(["h", "j", "k1", "k2", "k3", "j"], ["h", "j", "q1", "p"], ["h", "j", "q2", "p"])
    sk = skeleton.Skeleton(rs, "h", xy)
    # p has two neighbours, so the two ways to it form one balloon corridor from j back to j
    assert sk.nodes == {"h", "j"}
    balloons = sorted((c["stations"] for c in sk.corridors if c["a"] == "j" and c["b"] == "j"), key=len)
    assert balloons == [["j", "k1", "k2", "k3", "j"], ["j", "q1", "p", "q2", "j"]] or \
        balloons == [["j", "k3", "k2", "k1", "j"], ["j", "q2", "p", "q1", "j"]] or len(balloons) == 2
    assert len(sk.ends["j"]) == 5   # h-j, and each balloon twice
    # with a line beyond p, p becomes a junction and the two ways to it are two corridors between j and p
    rs2 = rs + routes_of(["h", "j", "q1", "p", "x"])
    rs2[-1]["id"] = "D"
    sk2 = skeleton.Skeleton(rs2, "h", dict(xy, x=(4000, 0)))
    assert sk2.nodes == {"h", "j", "p", "x"}
    assert sum(1 for c in sk2.corridors if {c["a"], c["b"]} == {"j", "p"}) == 2


# ---------------------------------------------------------------- whole maps with the blocks engine
import pathlib  # noqa: E402
import tempfile  # noqa: E402

import pandas as pd  # noqa: E402

import hub_map as hm  # noqa: E402
import station_route_map as base  # noqa: E402

# Doncaster-Sheffield shape: two ways from the hub to shf (via rc or via mh), lines beyond, other spokes
DON_XY = dict(hub=(0, 0), c1=(-1000, -200), mx=(-2000, -300), sw=(-2500, -400), rc=(-3000, -800), mh=(-1000, -1200),
              shf=(-2000, -1800), der=(-2000, -3000), yrk=(0, 2500), lds=(-3000, 2500), kgx=(2000, -3000), hul=(3000, 1000))
DON_SERVICES = [("N", "lds hub kgx", 6), ("N", "hub c1 mx sw rc mh shf", 2), ("X", "yrk hub mh shf der", 3),
                ("T", "hul hub mh shf", 2), ("G", "lds hub", 2)]
HORSHAM_SERVICES = [("T", "hub s a1 a2 m n1 n2", 6), ("T", "hub s b1 b2 b3 m n1 n2", 4), ("T", "hub s b1 b2 leaf", 3),
                    ("U", "hub s a1 m n2", 2)]


def toy_data(tmp, xy, services):
    tmp = pathlib.Path(tmp)
    pd.DataFrame([{"TiplocCode": c, "CrsCode": c, "StationName": c.upper(), "easting": e, "northing": n}
                  for c, (e, n) in xy.items()]).to_csv(tmp / "t.csv", index=False)
    rows = []
    for toc, pat, n in services:
        seq = pat.split()
        for day in ("20260908", "20260909"):
            for hour in range(9, 16):
                for k in range(max(1, n // 2)):
                    for s in (seq, seq[::-1]):
                        times = ["%02d:%02d:00" % (hour, (5 * i + 3 * k) % 60) for i in range(len(s))]
                        rows.append({"service_date": day, "rid": "%s%s%d%d%s" % (day, s[0], hour, k, toc), "toc": toc,
                                     "train_id": "1A00", "pattern": ",".join(s), "times": ",".join(times)})
    pd.DataFrame(rows).to_csv(tmp / "p.csv", index=False)
    return base.Data(str(tmp / "p.csv"), str(tmp / "t.csv"))


def build(data, hub, engine, monkeypatch, spacing=80):
    monkeypatch.setattr(hm, "SPACINGS", [spacing])
    monkeypatch.setattr(hm, "HUB_GAPS", [80])
    monkeypatch.setattr(hm, "ALL_SPACINGS", [spacing])
    monkeypatch.setattr(hm, "ALL_GAPS", [80])
    monkeypatch.setattr(hm, "STUB_METRIC", True)
    return hm.with_engine(hm.engine(engine), lambda: hm.build_map(data, None, hub, verbose=False, mode="all"))


def test_horsham_ring_has_no_fold_back(tmp_path, monkeypatch):
    data = toy_data(tmp_path, {k: v for k, v in XY.items()}, HORSHAM_SERVICES)
    svg, routes, spacing, report, omitted = build(data, "hub", "blocks", monkeypatch)
    assert len(routes) == 4
    assert report["retrace"] == 0 and report["stubs"] == 0 and report["crossings"] == 0
    assert report["dot_overlap"] == 0 and report["branch_overlap"] == 0
    assert report["blocks"][0]["cross"] == 0 and report["blocks"][0]["slack"] < 0.5


def test_second_corridor_is_drawn_where_the_tree_folds_back(tmp_path, monkeypatch):
    data = toy_data(tmp_path, DON_XY, DON_SERVICES)
    _svg, _routes, _sp, old, _ = build(data, "hub", "old", monkeypatch)
    _svg, routes, _sp, new, _ = build(data, "hub", "blocks", monkeypatch)
    assert old["stubs"] > 0                      # the tree engine runs the Rotherham route out and back
    assert new["stubs"] == 0 and new["retrace"] == 0 and new["crossings"] == 0
    assert len(routes) == 5 and hm.hard(new) < hm.hard(old)


def test_engine_toggle_restores_settings():
    saved = (hm.LAYOUT, hm.PDF_FILTER)
    assert hm.with_engine(hm.engine("blocks"), lambda: hm.LAYOUT) == "blocks"
    assert (hm.LAYOUT, hm.PDF_FILTER) == saved


def test_nested_blocks_and_a_route_through_the_hub_twice(tmp_path, monkeypatch):
    """A block the hub is not in (entered along a spoke), a second block beyond it along another spoke, and a
    loop that leaves the hub and returns to it (a route calling at the hub twice)."""
    xy = dict(hub=(0, 0), t1=(1000, 0), s=(2000, 0), a1=(3000, 400), b1=(3000, -400), m=(4000, 0), n1=(5000, 0),
              u1=(0, 1000), v=(0, 2000), v1=(-500, 3000), v2=(500, 3000), w=(0, 4000), x=(0, 5000),
              l1=(-1000, -1000), l2=(-2000, -1500), l3=(-2000, -500))
    # (different operators, so route discovery does not merge the near-identical patterns)
    services = [("T", "hub t1 s a1 m n1", 4), ("S", "hub t1 s b1 m n1", 2),
                ("U", "hub u1 v v1 w x", 3), ("V", "hub u1 v v2 w x", 2),
                ("L", "hub l1 l2 l3 hub", 2)]
    data = toy_data(tmp_path, xy, services)
    svg, routes, _sp, rep, _ = build(data, "hub", "blocks", monkeypatch)
    assert len(routes) == 5
    assert rep["retrace"] == 0 and rep["stubs"] == 0 and rep["crossings"] == 0 and rep["dot_overlap"] == 0
    assert len(rep["blocks"]) == 3 and all(b["cross"] == 0 and b["slack"] < 0.5 for b in rep["blocks"].values())
