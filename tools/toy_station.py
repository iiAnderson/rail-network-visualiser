"""Draw an invented station with either layout engine, no data needed: a quick look at how an engine handles loops.

    python tools/toy_station.py <network> <engine> [spacing] [mode]     e.g.  python tools/toy_station.py vic blocks

Networks: horsham (a split, two arms, a merge, a line beyond), don (Doncaster-Sheffield via Rotherham or Meadowhall),
clj / vic (a Clapham Junction-like network drawn from Clapham or from Victoria: the Kingston and Hounslow loops,
Horsham via Crawley or Dorking, Sutton via Wimbledon or Mitcham Junction), big (the same with about 30 routes).
Engines: old (tree), blocks. Writes maps/toy/<network>_<engine>.svg and .png (PNG needs cairosvg).
"""
import os, re, sys, pathlib, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import pandas as pd
import hub_map as hm, station_route_map as base

NETS = {
  # Horsham shape: hub, split s, arm a (fast) and arm b (with a branch to leaf) to m, then onward
  "horsham": (dict(hub=(0,0), s=(1000,0), a1=(2000,300), a2=(3000,300), m=(4000,0), b1=(2000,-400), b2=(3000,-400),
                   b3=(3500,-300), leaf=(3000,-900), n1=(5000,0), n2=(6000,0)),
              [("T", "hub s a1 a2 m n1 n2", 6), ("T", "hub s b1 b2 b3 m n1 n2", 4), ("T", "hub s b1 b2 leaf", 3),
               ("U", "hub s a1 m n2", 2)], "hub"),
  # Doncaster-Sheffield shape: two ways from the hub to shf (via r1 r2 or via mh), lines beyond, plus other spokes
  "don": (dict(hub=(0,0), c1=(-1000,-200), mx=(-2000,-300), sw=(-2500,-400), rc=(-3000,-800), mh=(-1000,-1200),
               shf=(-2000,-1800), der=(-2000,-3000), yrk=(0,2500), lds=(-3000,2500), kgx=(2000,-3000), hul=(3000,1000)),
          [("N", "lds hub kgx", 6), ("N", "hub c1 mx sw rc mh shf", 2), ("X", "yrk hub mh shf der", 3),
           ("T", "hul hub mh shf", 2), ("G", "lds hub", 2)], "hub"),
}

# a Clapham Junction-like network (km east/north of CLJ, roughly real): Kingston and Hounslow loops through the hub,
# Horsham via Crawley or Dorking, Sutton via Wimbledon or Mitcham Junction, the West London line, fast Brighton trains
CLJ_KM = dict(clj=(0, 0), wat=(2.5, 2.5), vic=(0.3, 2.3), wdc=(-0.3, -1.0), bal=(-0.2, -1.8), src=(0.7, -2.6), nrb=(1.2, -3.3),
              thh=(1.9, -4.0), sel=(2.6, -4.6), ecr=(3.8, -5.3), gtw=(0.5, -27), tbd=(1.5, -30), crw=(-1.5, -31), hrh=(-11, -33),
              mjn=(-0.2, -4.2), sut=(-3.4, -7.1), eps=(-8.2, -8.8), lhd=(-12.5, -11), dkg=(-12, -16), efd=(-1.3, -1.7),
              wim=(-4.3, -3.1), ray=(-5.3, -4.4), nem=(-7.3, -5.1), sur=(-9.8, -6.4), wok=(-29, -11), nbt=(-8.9, -5.7),
              kng=(-10, -5.5), ted=(-12.5, -4.4), twi=(-13, -2.5), rmd=(-11, 0.5), put=(-4.5, 0.3), wdt=(-2.2, 0.5),
              sta=(-26, -1.5), wnr=(-32, 1.5), bns=(-6, 0.2), bfd=(-10.5, 1.7), hou=(-14, -0.1), wht=(-14.6, -1.2),
              mot=(-6.5, -6.1), ham=(-12.5, -7.8), imw=(-0.5, 1.7), wbp=(-1.3, 3.0), kol=(-2, 4.0), spb=(-2.6, 5.2),
              wij=(-4.5, 7.5), wfj=(-6, 30), cat=(5, -14), btn=(2, -58), hhe=(2, -46))
CLJ_XY = {k: (530000 + 1000 * e, 175000 + 1000 * n) for k, (e, n) in CLJ_KM.items()}
CLJ_SERVICES = [("SW", "wat clj efd wim ray nem sur wok", 4), ("SW", "wat clj efd wim ray nem nbt kng ted twi rmd put wdt clj wat", 2),
                ("SW", "wat clj put rmd twi sta wnr", 2), ("SW", "wat clj put bns bfd hou wht twi rmd put clj wat", 2),
                ("SN", "vic clj wdc bal src nrb thh sel ecr gtw tbd crw hrh", 2), ("SN", "vic clj bal src mjn sut eps lhd dkg hrh", 2),
                ("SN", "vic clj ecr gtw btn", 3), ("SN", "vic clj ecr cat", 2), ("SN", "vic clj wdc bal src mjn sut", 2),
                ("SW", "wat clj efd wim sut eps", 2), ("SW", "wat clj efd wim ray mot ham", 2), ("LO", "clj imw wbp kol spb wij wfj", 2),
                ("SN", "vic clj wdc bal src nrb thh sel ecr gtw tbd hhe btn", 2)]
NETS["clj"] = (CLJ_XY, CLJ_SERVICES, "clj")
NETS["vic"] = (CLJ_XY, CLJ_SERVICES, "vic")

# a bigger Clapham: the same plus semi-fast variants and more branches (about 30 routes), to time the engine
BIG_KM = dict(CLJ_KM, epd=(-3.6, -9.5), chs=(-8.5, -8.0), shp=(-14.5, -5.5), gld=(-22, -17), cbm=(-11, -9.5),
              tat=(-1.5, -12), mkc=(-12, 60), rdg=(-45, 3), cpl=(2.5, -3.9), sth=(1.5, -2.8), bkh=(-1.5, 2.2),
              hhe=(2, -46), lwk=(3.5, -2.5), eph=(-8.5, -8.9))
BIG_XY = {k: (530000 + 1000 * e, 175000 + 1000 * n) for k, (e, n) in BIG_KM.items()}
BIG_SERVICES = CLJ_SERVICES + [
    ("SW", "wat clj wim sur wok", 2), ("SW", "wat clj efd wim ray mot chs", 2), ("SW", "wat clj wim ray nem sur cbm gld", 2),
    ("SW", "wat clj efd wim ray nem nbt kng shp", 2), ("SW", "wat clj put rmd twi sta rdg", 2), ("SN", "vic clj bal src mjn sut epd", 2),
    ("SN", "vic clj wdc bal src sel ecr tat", 2), ("SN", "vic clj bal sth cpl", 2), ("LO", "clj imw wbp kol spb wij mkc", 2),
    ("SE", "vic clj wdc bal src nrb thh sel ecr", 3), ("SW", "wat clj efd wim sut eph", 1), ("SN", "vic clj ecr gtw tbd hhe btn", 2),
    ("SN", "vic clj wdc bal src mjn sut eps lhd dkg", 1), ("SW", "wat clj wdt put bns bfd hou wht twi", 1), ("GX", "vic clj gtw btn", 2),
    ("SW", "wat clj efd wim ray nem sur", 2), ("SN", "vic clj bal src nrb thh sel ecr gtw", 1), ("LO", "clj imw wbp kol", 1)]
NETS["big"] = (BIG_XY, BIG_SERVICES, "clj")


def make(tmp, xy, services):
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

if __name__ == "__main__":
    net, eng = sys.argv[1], sys.argv[2]
    sp = float(sys.argv[3]) if len(sys.argv) > 3 else 80
    mode = sys.argv[4] if len(sys.argv) > 4 else "all"
    xy, services, hub = NETS[net]
    data = make(pathlib.Path(tempfile.mkdtemp()), xy, services)
    hm.set_engine(hm.engine(eng))
    hm.STUB_METRIC = True
    hm.SPACINGS, hm.HUB_GAPS, hm.ALL_SPACINGS, hm.ALL_GAPS = [sp], [80], [sp], [80]
    svg, routes, spacing, report, omitted = hm.build_map(data, None, hub, verbose=True, mode=mode)
    print("routes drawn", len(routes), "omitted", omitted)
    print({k: v for k, v in report.items() if k != "where"})
    print("hard", hm.hard(report))
    out = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / ".." / "maps" / "toy"
    out.mkdir(parents=True, exist_ok=True)
    out = out / ("%s_%s%s" % (net, eng, "" if mode == "all" else "_" + mode))
    out.with_suffix(".svg").write_text(svg)
    try:
        import cairosvg
    except ImportError:
        print("wrote", out.with_suffix(".svg"), "(pip install cairosvg for a PNG)")
    else:   # cairosvg cannot draw paint-order, so the label halo is dropped for the preview
        png = re.sub(r' stroke="#fff" stroke-width="[45]"( stroke-linejoin="round")? paint-order="stroke"', "", svg)
        cairosvg.svg2png(bytestring=png.encode(), write_to=str(out.with_suffix(".png")), output_width=1100)
        print("wrote", out.with_suffix(".png"))
