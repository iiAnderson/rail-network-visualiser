# Plan: a national route map (no central station)

Status: **step 1 in progress** (`src/national_edges.py` builds the edge/tph table; see the findings in
section 9). Athena was not needed: `national_patterns.csv` is already local (cost $0). Written for an agent picking this up cold. Read `README.md` first, then
`src/hub_map.py` (the per-station engine) and `src/nr_route_map.py` (`Network`, PDF graph helpers).

## 1. Goal

One diagram of the whole UK passenger network in the same visual language as the per-station maps:
coloured lanes per route, dots where a route stops, tph on stations and on lines, interchange
capsules only at major stations. It is a *network* map, so there is no hub to radiate from.

The data window is the same "normal" service: Tue–Thu 09:00–16:00, Sep 2026 (`national_patterns.csv`,
216k services, 9 days).

## 2. Rules that carry over unchanged

These are the user's rules from the hub maps; every national output must be measured against them
with the same report keys (`dot_overlap`, `label_collision`, `crossings`, `branch_overlap`,
`dot_on_line`, `leader_lines`):

1. No two dots overlap (>= `DOT_GAP` = 8.4 px apart).
2. A line never runs under another for any length (different routes keep a lane gap, `PITCH` 9.5 px).
3. Interchange capsule only at a major station: >= `MAJOR_TPH` (3) calling **and** two calling routes
   that go different ways. None where a train does not stop.
4. No crossings except where routes genuinely diverge/merge (the PDF graph is planar, so a lane
   ordering that follows "where each route goes next" gives none).
5. No overlapping text, lines or dots; every label starts within `LABEL_MAX` (30 px) of its own
   station's dots and is closer to them than to any other station's, else a thin leader line.
6. Routes drawn in full to their termini. Only stations that some drawn route stops at are shown.
7. Keep the label engine's behaviour: clashing labels are re-placed first (`run` loop in `render`),
   the hub-style big label is not needed here.

## 3. How tph is defined without a hub

* **Station tph** = trains per hour *each way* calling there (same as `generate_all.rank_stations`:
  calls in window / slots / 2). This already exists.
* **Line tph (a segment between two adjacent shown stations)** = services that traverse the segment
  (call or pass) in window / slots / 2. Count by walking each service's pattern along the PDF graph
  (see 4.2). A lane is one route; the segment's tph is the sum of its lanes' tph. Print it on long
  segments and where the value changes, not on every edge.
* A "route" nationally = the same clustering as `discover_routes` (operator + stopping pattern,
  Jaccard merge) but **not anchored on a hub**: cluster whole patterns, not the window around a hub.
  Expect thousands of routes. See 4.3 for how to keep that drawable.

## 4. Approach

### 4.1 Layout: use the PDF's own geometry
The hub maps use an evenly spaced tree because the PDF-spaced layouts looked worse *around one hub*.
A national map has no hub and the PDF is already a clean octilinear schematic of exactly this
network, so here **use `nr_schematic.json` positions directly** (nodes have `x, y`; edges have `pts`
polylines). `Network.match` maps CRS -> node. Do not re-derive a layout. Work in PDF units and scale
(~x3.5) so `PITCH`/`DOT_R` are legible.

Consequences: no spanning tree, no `layout()`, no spacing search. The planarity and distances come
for free; the work is lanes, dots, labels and selection.

### 4.2 Put every service on the graph
Reuse `hub_map.expand_via_pdf` / `Network.path` (Dijkstra between consecutive calling stops, with the
`close_triangles` fallback). Output per service: the ordered node/edge list. Aggregate per edge:
`{route_id: tph}`. Build the edge->routes table once; cache it as a pickle in the data folder
(git-ignored). Verify: sum of tph on edges around a station is consistent with its call tph.

### 4.3 Which routes get a lane (the real design problem)
2,000+ patterns cannot each have a lane. Collapse in this order and measure the effect:
1. Merge near-identical patterns (existing Jaccard >= 0.5 and >= 80% of stops).
2. **Colour by operator, not by route** (about 25 colours). Lanes within a bundle are then
   *operator-services*, ordered by where they go next (see 4.4). Keep the route letters only at
   termini of the top N routes, otherwise label termini with the operator colour.
3. On an edge, merge same-operator lanes whose onward ways agree into one lane with summed tph and
   a small tph-width cue. Cap lanes per edge (suggest 8); beyond that merge the two thinnest
   lowest-tph lanes of the same operator, then of any operator, and record that it happened.
4. Drop lanes < `MIN_TPH` (0.3), as now.

This changes rule 6 to "routes drawn in full" meaning *each operator's service pattern reaches
its termini*, which is the best a single sheet allows. State this on the map legend.

### 4.4 Lane order along an edge
Same rule as `render` in `hub_map.py`: order lanes by pre-order position of where each route goes
next. Without a tree, define "next" per edge direction by the angle of the route's next edge at
the downstream junction (sort lanes by signed turning angle left->right), and keep absolute
offsets along a bundle so lines do not wiggle after a junction. Resolve conflicts at junction
nodes with the same "peel off outward" logic. Test on a toy network first (`tests/test_smoke.py`
shows the style).

### 4.5 Stations, dots, capsules
* Shown stations: those where a *kept* lane stops (rule 6).
* Dots: one per (lane, station), placed at the lane offset; spread inside a capsule along its
  axis as `render` does (`DOT_GAP + 1.6`).
* Capsules: the same major-station test as 3 above, computed on the PDF graph neighbours.
  Expect ~100–200 capsules. Check there are none at stations where the called lanes are parallel.

### 4.6 Labels
Reuse the candidate generator `nrm.candidates` and the `run`/`candidates_for` machinery from
`render` (STRtree on obstacles, per-character `text_w`, re-place-failed-first loop, leader lines).
Differences: thousands of stations, so
* **Tile it.** Place labels per region tile (see 4.7) with a margin, so STRtree queries stay small.
* Label priority by station tph descending; low-tph stations at national zoom may be hidden by
  zoom level in the HTML viewer (below), not dropped from the data.
* Keep the 30 px / ownership rules at every zoom level.

### 4.7 Output: a zoomable page, plus regional SVGs
A single 20k-px SVG is not usable. Produce:
1. `national.svg` at low detail (capsules, long-distance lanes, major stations only) for overview.
2. Regional SVGs at full detail, cut along the PDF's own natural gaps: Scotland, North East,
   North West, Yorkshire/Humber, Midlands, Wales, South West, East of England, London & South East
   (London needs an inset because the PDF is densest there; treat it as its own scale).
3. A viewer page (`web/national.html`) that loads regional SVGs, supports pan/zoom, and shows labels
   by tph threshold. The existing `web/index.html` is the template style.
Cut lines must fall on edges between stations; draw a "continues to X" stub with the route colour
and tph there.

## 5. Build order (each step has a checkable result)

1. **Graph + edge tph table.** Script `src/national_edges.py`. Check: top-20 busiest segments are
   sensible (e.g. London–Reading, Clapham Jn–Waterloo). Unit-test tph summation on the toy network.
2. **Lane model.** Operator-lane merging and cap (4.3). Check: no edge exceeds the lane cap; report
   how many merges happened; spot-check the Birmingham–Wolverhampton corridor against the Birmingham
   hub map.
3. **Lane geometry.** Offsets along edges and through junctions (4.4). Check with the existing
   `crossings` / `branch_overlap` measures run over *all* edges.
4. **Dots + capsules** (4.5). Check: `dot_overlap == 0`; capsule count and a hand list of ten known
   interchanges (Birmingham New St, Crewe, Doncaster, Reading, York, Preston, Carlisle, Newcastle,
   Peterborough, Didcot).
5. **Labels per tile** (4.6). Check: `label_collision == 0` per tile or list the failures; leader
   count reported.
6. **Regional SVGs + viewer** (4.7). Visual review of every region by eye (the report is not
   enough; the user judges by looking). View with `cairosvg` -> PNG, crop at 3x like earlier
   debugging.
7. **Run it end to end** with a timeout/crash-safe batch (see 6).
8. README section, add one example, commit. Do not commit generated maps or the PDF-derived graph.

## 6. Pitfalls already met (do not rediscover)

* **GEOS crashes** on `buffer()` of long self-retracing polylines: always use `safe_buffers`
  (chunked, 10 vertices). A crash kills the process with no Python error.
* A forked `multiprocessing.Pool` **hangs forever** if one worker dies. `generate_all.py` now
  starts one process per task with a timeout and crash detection; do the same for regional runs.
* **Duplicate dots** in the PDF within ~2 pt must be merged (`MERGE = 2.0` in
  `extract_nr_schematic.py`) and large interchange capsules need a larger attach tolerance, or
  routes get tangled around London.
* Stations missing from `tiplocs.csv` (e.g. Elizabeth line) show raw TIPLOC names. Fix the name map
  first (`sql/tiplocs.sql`) or national output will look broken in London.
* Multi-TIPLOC stations share one CRS (`Data` already folds them); use CRS keys everywhere.
* Use only hard rule failures when deciding to keep a route (`hub_map.hard`); leader lines are a
  mild charge (0.15 each), counting them made every added route look costly.
* PDF copyright: it is (c) Rail Delivery Group. Only the extraction script is committed; keep
  `nr_schematic.json` and anything derived from its geometry out of git.
* Environment: `python3 -m venv` + `pip install -r requirements.txt` (pandas, numpy, shapely>=2);
  `pymupdf` only for the extraction step; `cairosvg` for previews.

## 7. Known weak points to be honest about

* Loops (two corridors between the same places) are drawn properly here, because the PDF graph is
  used as-is, unlike the hub maps, where the quieter corridor becomes a spur.
* London will be the hardest area: many operators over shared track. Expect to need the inset and a
  lower lane cap; say so on the map rather than hiding lines.
* Operator colours remove the route letters. If the user wants letters nationally, define them per
  top-N long-distance route and keep the rest as operator colour.

## 8. Open questions for the user (ask before starting step 3)

1. Operator colours with merged lanes, or a distinct colour per route even if the map is huge?
2. Acceptable to ship regional sheets + a zoomable viewer instead of one image?
3. Include stations below 2 tph at national zoom (hidden until zoomed), or leave them out as now?

## 9. Findings from step 1 (read before step 2)

* Matching: PDF interchange labels run together ("East Croydon East Croydon", "Knightsbridge Gloucester
  Road South Kensington"). `Network.match` now accepts a label containing the whole name near the predicted
  position, `ALIASES` in `national_edges.py` covers Elizabeth line TIPLOCs and a few others, and a
  nearest-unclaimed-node rescue fills the rest. 2,533 of 2,801 stations now match; ~78k hops (of ~1.5M)
  still touch an unmatched station (Liverpool Street Low Level, Willesden Jn Low Level, Edinburgh Airport,
  Cnnbell ...).
* **The PDF graph also draws the Underground and is not faithful in London.** Plain Dijkstra sent
  Victoria -> Clapham Jn via Earls Court (tube) and Waterloo -> Clapham Jn via Herne Hill/Balham. Edges
  that no train uses between adjacent stops now cost x4 (`UNCONFIRMED`), which fixed some, not all. London
  segment tph in the table is therefore unreliable (e.g. Wandsworth Common-Balham 41 tph is too high).
  Outside London the busiest segments look right (Haymarket-Edinburgh ~26 tph).
* 405 hops have no PDF path at all (Ryde Pier Head-Portsmouth Harbour, Darlington-North Road, Hexham-Haydon
  Bridge, Oxford-Tackley).
