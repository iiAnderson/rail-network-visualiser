# Plan: drawing loops properly

Status: proposal for review. Nothing here is implemented. Written so another agent could pick it up cold.
Read `README.md`, then `src/station_route_map.py` (`build_tree`, `find_chords`, `bypass_loops`,
`_bypass_one`, `route_tree_nodes`) and `src/hub_map.py` (`render`, the retrace count, `choose_routes`).

## 1. The problem

A "loop" is two corridors that join the same places (Horsham via Dorking or via Crawley; Euston to
Stafford via Northampton, Trent Valley or Birmingham; Doncaster to Sheffield via Rotherham or
Meadowhall). Today the layout cannot draw a loop. It works like this:

1. `build_tree` keeps a spanning tree of the busiest edges. Every dropped edge is a *chord*.
2. A chord's cycle is closed afterwards by `_bypass_one`: the lighter arm of the cycle is pinned as
   a parallel line beside the other arm and rejoins "MERGE_GAP before the station it runs to".

That is a patch on a tree, and it fails in recognisable ways:

| Symptom (where seen) | Cause |
|---|---|
| Station sits on a dead-end stub that looks like a terminus (Crawley, Horsham map) | Which edge Kruskal drops decides which station ends up a leaf; the loop then closes *before* that leaf, so a route calling there must fold back |
| Route zig-zags out and back (Wilmslow, Stockport on the Manchester route) | Chord to a leaf, or two loops sharing stations (`used` check in `bypass_loops` skips them) |
| Loop silently not drawn, route turns back on itself | `_bypass_one` returns `_why(2..6)`: not enough room, tight corners, a neighbouring branch in the way, bypass stations too close |
| Crossings between routes using mixed combinations of corridors (Euston: Birmingham/Stafford/Crewe) | Only one chord is planned per cycle; a route that switches corridor mid-way crosses the bundle |
| Chord to an ancestor skipped (`if S in (x, y): continue`) | No arm to bypass; patched in this repo by `follow_tree`, which only covers the pure-ancestor case |

Numbers (current `maps/`, page 1 plus later pages): 1,612 page maps, 309 with at least one turn-back,
170 with crossings. The stations people look at most (Euston, Victoria, Clapham Junction,
Birmingham, Sheffield, Leeds, Manchester) are the ones affected.

What was tried and did **not** help (do not repeat): weighting the spanning tree towards long routes
(Euston's tree is identical at every setting); raising the failure tolerance for the first route to
each destination (readable maps degrade, 8 gave 11 crossings at Victoria); merging at the dead-end
station instead of 40 px before it; preferring the dead-end side as the bypass (Clapham crossings
79 to 103). The tree-plus-patch structure is the limit.

## 2. Idea: lay out the loops first, as rings

Most rail loops are *series-parallel*: two or more corridors leave one station S and meet again at
one station M, possibly with branches hanging off the arms. Draw them as such, instead of drawing
a tree and repairing it.

For the Crawley case: S is where the Gatwick line and the Dorking line split, M is Three Bridges.
Arm A (the straight one) goes via Gatwick, arm B via Dorking, Horsham and Crawley. Crawley is the
last station on arm B before M, so it sits on the bypass, not on a stub, and nothing folds back.

### 2.1 Structure
1. **Station graph.** Edges are the consecutive-stop steps of the drawn routes (after
   `expand_via_pdf`), weight = tph through the edge, as now.
2. **Blocks.** Find the 2-edge-connected components (the places that contain loops). Everything
   outside a block is still a tree and is laid out exactly as today.
3. **Series-parallel reduction** inside a block: repeatedly (a) merge a degree-2 station into a
   series chain, (b) merge parallel chains between the same two stations into one *parallel node*
   with k arms. The result is a tree of components (S, M, arms, each arm possibly containing
   another parallel node, so nested loops come out naturally).
4. **Non-reducible blocks** (a ladder, a mesh): fall back to the current tree + chords for that
   block only, and record it in the report (`loops_fallback`), so it is visible.

### 2.2 Layout of a parallel node
- The block is entered along the direction of its parent and left in the same direction.
- **Spine arm**: the heaviest (most tph) arm runs straight from S to M.
- **Other arms**: offset beside the spine, leaving S and rejoining M by 45 degree diagonals (the
  same visual language as today's bypass), on the side chosen by branch counts, real geography and
  where the routes go after M (the existing side scoring, moved over unchanged).
- **M is a real station position shared by every arm.** There is no "merge before v". Stations
  beyond M continue on the spine direction. S and M are placed by the parent layout like any
  node, with the edge S to M's length set by the block (see 2.3).
- **Stations on arms** are spaced evenly along their arm. Branches off an arm leave to the
  outer side, away from the other arms (as the bypass does today).
- **Parallel lanes**: a route uses the arm it really takes, and keeps its lane offset through S
  and M, so lanes only change side where routes diverge (existing lane-order rule, applied to a
  node with k arms instead of a tree branch).

### 2.3 Sizing
Each arm has a required length from its own station count and branches (`MIN_STEP * SPACING` per
station, as now). The block's length is the longest arm. Shorter arms and the spine take their
stations at a wider step; the spine is stretched only if it is the longest arm. This removes the
current "stretch and relayout, up to the cap" loop (`res["need"]`, `MAX_STRETCH`) for loops.

### 2.4 Where a route goes
`route_tree_nodes` walks a route over tree edges plus chords. With blocks, a route simply walks its
real stations; inside a block each step is on an arm of the parallel node or at S/M. The retrace
count then measures real fold-backs (a route reversing along an arm), not layout leftovers.

## 3. Build order (each step has a check)

1. **Toy tests first** (`tests/test_smoke.py` style): diamond; diamond with a leaf on one arm (the
   Crawley shape); nested diamond; two loops sharing a station; a ladder (must fall back and say
   so). Check: no crossings, `retrace == 0`, stations on the arm they belong to.
2. **Block finder + SP reduction** as a pure function on the edge list, with its own tests. No
   drawing yet. Check on real data: how many of the loop-containing stations reduce fully? This is
   the number that decides whether the plan is worth it. Run it over all 1,307 stations and report
   it in the PR before step 3.
3. **Layout for one parallel node** (2 arms, no branches), wired in behind a flag
   (`LOOP_ENGINE = "rings" | "tree"`).
4. **Branches on arms, k arms, nested nodes.**
5. **Lanes through S and M**, using the existing lane-order code.
6. **Selection per station**: render with both engines in `build_map`/`_render_best` and keep
   whichever has the lower `hard()` score. The new engine can then never make a map worse, and
   the comparison tells us where it wins.
7. **Regression harness**: the manifest comparison used to produce the statistics in this
   conversation (clean count, retrace, crossings, worse/better count against the previous set),
   saved as `tools/compare_sets.py`.
8. **Full regeneration**, README update (the loops notes), commit.

## 4. Success criteria

Measured on the same 1,307 stations and the same data:
- maps with a turn-back: 309 down to under 100;
- crossings: no increase overall, and no station worse by more than 3 `hard()` points unless the
  old engine's result is kept (step 6 guarantees that);
- named checks by eye: Horsham/Crawley (no stub), Clapham Junction (Victoria side), Euston page 1,
  Manchester Piccadilly (Wilmslow/Stockport), Sheffield, Bridgend, Doncaster, Reading;
- run time: no station slower than 5 minutes (the batch has a 20 minute limit and the current
  full run takes about 20 minutes in total).

If step 2 shows that fewer than about 60% of loop-containing stations reduce to series-parallel,
stop and rethink before building the layout.

## 5. Risks

- **Not series-parallel in practice.** Real rail graphs have crossovers, triangles and routes that
  use an arm half-way. The PDF-expanded paths add stations a route never calls at. Mitigation: the
  step 2 measurement, the per-station fallback, and a cap on how many "mixed" routes an arm may
  carry before the block is declared non-reducible.
- **Geography versus hub-centred layout.** Horsham is reached from Clapham via Dorking but really
  lies south-west of Crawley, so one arm is genuinely a U-turn. The ring layout shows it as a
  parallel arm that rejoins, which is honest, but it will still look odd for a reader who knows the
  map. This is a presentation choice, not a bug (see question 1).
- **Everything that depends on the tree** (`assign_ordered`, wedge constraints, label placement,
  `lane order by pre-order`) assumes one parent per node. A block must present itself to those as
  a single super-edge. Plan time for that interface; it is the largest unknown.
- **Time.** Layout attempts per station multiply if both engines are tried. The big-hub probe cap
  (`BIG_HUB_ROUTES`) already exists for the route picker; the same approach may be needed.

## 6. Out of scope

Changing which routes are shown or the pages (done); the national map (`PLAN_national_map.md`,
which uses the PDF's own geometry and does not need this); new data.

## 7. Questions for the owner

1. Where the real geography is a U (Horsham/Crawley), is a parallel arm that rejoins acceptable, or
   do you want the diagram to follow the compass bearings more closely, even if that means more
   crossings?
2. For blocks that are not series-parallel: keep today's behaviour and flag them, or move the
   routes that cannot be placed onto a later page?
3. Which stations should decide whether this worked (my list above: Horsham, Clapham Junction,
   Euston, Manchester Piccadilly, Sheffield)? Anything missing?
4. Is "never worse than the old engine, chosen per station" the right safety net, even though it
   makes every build slower?
