# rail-network-visualiser

Pick a station and get an Avanti-style route map of the trains that serve it: every route drawn in
full to its terminus as a coloured lane, a dot wherever a route stops, route letters at the ends,
and the **trains per hour (tph)** on every station and route.

![Bristol Temple Meads](docs/examples/BRI.svg)

More examples: [Doncaster](docs/examples/DON.svg) · [Reading](docs/examples/RDG.svg)

The maps are built from real timetables (the "normal" service: Tuesday–Thursday, 09:00–16:00, so no
peaks and no early/late workings). The normal day is the **median over many sampled days** (every
Tue–Thu from June to mid-October 2026), so engineering works and diversions on a few days do not change it.
The maps are laid out automatically. They are schematic: distances are
not to scale, but the order of stations along every route is real.

## How it works

```
services (CSV)  ──►  routes  ──►  station graph  ──►  tree layout  ──►  lanes + labels  ──►  SVG
 one row per          clustered     which stations     octilinear         rule checks, then
 train that ran       by stopping   a fast train       spokes from the    pick the tightest
                      pattern       passes             hub                spacing that passes
```

1. **Routes** (`station_route_map.discover_routes`). Every service that calls at the hub inside the
   window is grouped by operator and stopping pattern. Patterns that mostly overlap are merged, so a
   short working counts towards the route it is part of. tph is trains per hour *each way*. At most
   24 routes are considered, and rarer ones (< 0.3 tph) are dropped. The busiest 6 are always drawn; each
   further route is test-rendered and kept only if it adds at most one rule failure (the busiest route to
   each distinct far terminus is allowed more, so a destination is not dropped lightly). Routes that
   would tangle the map go to **further pages**: the same hub drawn again with the services the previous
   page left out, until every grouped pattern (down to 0.1 tph) is on some page (at most 4).
2. **Stations a fast train passes.** Only stations that some drawn route stops at appear. Between two
   stops of a route, the stations it passes (and another route stops at) are found from the
   National Rail route map network, and from a "flat triangle" rule when that is unavailable.
3. **Layout** (`station_route_map.layout`). The graph becomes a spanning tree that keeps the most
   traffic (the busier of two parallel corridors stays straight; the quieter one hangs off it as a
   spur). Branches leave the hub in 45° directions that keep their true angular order (so two branches
   never swap sides and cross), as close as possible to their real compass bearings.
4. **Lanes.** Routes sharing track run as parallel lanes. Lane order follows where each route goes
   *next*, so lines only change side where they diverge. A lane keeps its offset along a bundle, so
   nothing wiggles after a junction.
5. **Labels.** Placed greedily around the real lines, starting within 30 px of their own dots and
   closer to them than to any other station's dots; a thin leader line is drawn when that is not
   possible.
6. **Spacing search** (`hub_map.build_map`). The map is laid out at a series of spacings (250 px down
   to 47 px between stations) and hub gaps (80/140/200 px). Each is checked against the rules below;
   the fewest hard failures wins, then the fewest leader lines, then the tightest spacing.

### The rules (all measured, and printed for every map)

| Rule | Checked as |
|---|---|
| No dot overlaps another | dots at least 8.4 px apart |
| Interchange marks only at major stations | a capsule needs ≥ 3 tph calling **and** routes that both stop there going different ways. A station merely served by many routes is not marked, and routes that do not stop get no mark |
| No line hidden under another | lines of different routes never run within a lane gap of each other |
| No crossings | none outside the hub and interchange capsules |
| No overlapping text | labels (including the hub's) clear of lines, dots and each other, and tied to their own station. Text widths are estimated per character; labels that clash are re-placed first until none do |
| Routes drawn in full | to the real terminus; up to 24 routes, as many as stay clean |

The per-map report looks like
`{'dot_overlap': 0, 'label_collision': 0, 'crossings': 0, 'branch_overlap': 0, 'dot_on_line': 2, 'leader_lines': 49}`.

## Quick start

```bash
git clone git@github.com:iiAnderson/rail-network-visualiser.git && cd rail-network-visualiser
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt scipy
python -m pytest tests            # smoke test on an invented network, no data needed

# one station (CRS code), then open maps/BRI.svg
python src/hub_map.py BRI --patterns data/national_patterns_days.csv --tiplocs data/tiplocs.csv \
    --network data/nr_schematic.json

# many stations, in parallel, resumable; also writes maps/index.html (a station selector)
python src/generate_all.py --patterns data/national_patterns_days.csv --tiplocs data/tiplocs.csv \
    --network data/nr_schematic.json --top 300 --workers 6
open maps/index.html
```

A single busy station takes 1–6 minutes (the spacing search is the cost), so `generate_all.py`
runs several at once.

## Inputs

`data/` is git-ignored: nothing in it is committed.

**Two input formats.** Either `national_patterns.csv` (one row per service that ran, a few days) or, better,
`national_patterns_days.csv` (one row per distinct timetabled train, with the days it ran; use many days).
The second is turned into a normal day: for every operator + calling pattern, the median number of trains
per day over the days it ran, dropped if it ran on fewer than 40% of the days, with real timings taken from
a day that matches. A line that was closed on a few of the sampled days therefore keeps its normal service.

**`national_patterns.csv`**: one row per passenger service that ran, with these columns:

| column | meaning |
|---|---|
| `service_date`, `rid`, `train_id` | identifiers (`service_date` is used to count days) |
| `toc` | operator code, e.g. `GW` |
| `pattern` | comma-separated **TIPLOCs** of the calling points, in order |
| `times` | comma-separated `HH:MM:SS` for each calling point (departure, else arrival) |

**`tiplocs.csv`**: `TiplocCode, CrsCode, StationName, easting, northing` (OSGB grid, metres). The
CRS code is what you pass on the command line; coordinates are used to point each branch the right
way. Stations missing here still work, but get their TIPLOC as a name and no bearing.

**`national_patterns_days.csv`**: `toc, pattern, times, days` (the TIPLOC calling points, their times, and a
comma-separated list of the `YYYYMMDD` dates the train ran).

[`sql/national_patterns_grouped.sql`](sql/national_patterns_grouped.sql) (many days) or
[`sql/national_patterns.sql`](sql/national_patterns.sql) (a few days), and [`sql/tiplocs.sql`](sql/tiplocs.sql),
produce these from the `normalised_v1` and `tiplocs` tables of the
author's `darwin-storage` pipeline (National Rail Darwin feed, on AWS Athena);
[`tools/fetch_athena_weeks.sh`](tools/fetch_athena_weeks.sh) (all Tue–Thu between two dates; about
370 MB scanned for 60 days) or [`tools/fetch_athena.sh`](tools/fetch_athena.sh) runs them. Any source with the same columns works.
Choose typical midweek days from a month with no big disruption.

**`nr_schematic.json`** (optional): the line network read from the National Rail "All Stations Route
Map" PDF, used only to work out which stations a fast service passes between two stops. Without it
the maps still build, using the flat-triangle rule alone (a little less accurate on busy
corridors). Build it from your own copy of the PDF:

```bash
python3 -m venv /tmp/nrvenv && /tmp/nrvenv/bin/pip install pymupdf
/tmp/nrvenv/bin/python -I src/extract_nr_schematic.py National_Rail_All_Stations_Route_Map.pdf data/nr_schematic.json
```

## Published maps

Every station is published at **<https://rail.robbiea.co.uk/routes>** (`/routes#CLJ` for one station), one of
the visualisations on the rail site. Rebuild and publish with `tools/site.sh release`; the process, what this
repository owns on that site and how to roll back are in [docs/DEPLOY.md](docs/DEPLOY.md).

## Repository layout

```
src/hub_map.py               the map builder and rule checks (start here)
src/station_route_map.py     data loading, route discovery, tree layout, shared helpers
src/skeleton.py              junctions and corridors of a station graph; src/loops.py finds its blocks
src/block_layout.py          the blocks engine: octilinear layout of each block (linear program), spokes elsewhere
src/block_render.py          lanes on the drawn graph; drawing is shared with the tree engine (hub_map.draw_scene)
src/nr_route_map.py          loads the PDF-derived network; label placement helpers
src/extract_nr_schematic.py  one-off: PDF -> data/nr_schematic.json
src/generate_all.py          batch runner + station selector page
sql/  tools/                 how to get the input CSVs
tools/compare_engines.py     both engines on CLJ, VIC, LDS and the toy networks, one comparable score
tools/site.sh                rebuild every station and publish to rail.robbiea.co.uk/routes (docs/DEPLOY.md)
web/index.html               the station viewer (written beside the maps; also the page /routes on the site)
docs/examples/               three finished maps
tests/                       smoke test
```

`nr_route_map.py` also contains an earlier experiment that drew the maps at the PDF's own
spacing. It is kept for its helper code; the maps were clearer on the evenly spaced tree layout.

## Tuning

Everything is a constant at the top of `src/hub_map.py` / `src/station_route_map.py`:
`WINDOW` (hours), `MAX_ROUTES`, `MIN_ROUTES`, `ROUTE_TOLERANCE`, `MIN_TPH`, `HUB_GAPS`, `SPACINGS`, `MAJOR_TPH`, `LABEL_MAX`, `PITCH` (lane gap).

## Layout engines

`--engine old` (the default) lays the station graph out as a spanning tree with patched loops. Where two
corridors join the same places (Horsham via Dorking or via Crawley, Doncaster–Sheffield via Rotherham or via
Meadowhall) a tree has no loop: the quieter corridor becomes a spur and a route on it folds back, or is left
off the page. `--engine blocks` lays out the cyclic part of the graph as a graph instead
(`plans/BLOCK_LAYOUT.md`): the station graph is split into 2-edge-connected blocks and bridges; inside a
block, chains of stations collapse to corridors between junctions; each junction's corridor ends take
compass directions in their real angular order; a linear program places the junctions and sizes the
corridor segments so every cycle closes and every corridor fits its stations, with separation constraints
added where two corridors come too close; stations are spaced along their corridor; the bridges hang off
as spokes exactly as the tree engine draws them. Routes walk their real stations, so a fold-back is only
ever a real reversal in the timetable. Lanes on a corridor are ordered by where each route goes after the
far junction (then where it came from), and a crossing inside a junction counts half (`junction_crossings`).

```bash
python src/hub_map.py VIC CLJ --engine blocks --mode all --patterns ... --tiplocs ... --network ...
python src/generate_all.py --engine blocks ...
```

`pip install scipy` first (the linear program). `hub_map.build_map_best` draws a station with every engine
and keeps the one with the fewest hard failures.

## Known limitations

- The `old` engine draws no loops: where two corridors join the same places the quieter corridor becomes a
  spur, and routes that need the loop are left out rather than drawn badly. Use `--engine blocks` for
  those stations.
- Very dense areas (Reading–Swindon–Gloucester, London) can still end with a few label clashes or
  one crossing; the report says how many. Stations where the report lists failures are best checked by eye.
- Stations not in `tiplocs.csv` (e.g. the Elizabeth line) show a TIPLOC as their name.
- One day-type only (midweek daytime). tph is an average over those hours of the normal day, so it is fractional
  (1.3 tph, not "2"); it is not a timetable promise.

## Data and licensing

Code: GPL-3.0 (see `LICENSE`). The data is not part of this repository:

- Train data comes from the National Rail Darwin feed (Open Rail Data); check its terms before
  republishing maps built from it.
- The route map PDF is © Rail Delivery Group and "must not be reproduced in any form without
  permission". This repo ships only the extraction script, not the PDF nor anything extracted from it.
