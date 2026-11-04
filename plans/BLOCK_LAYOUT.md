# Plan: lay out the cyclic part of a station's network as a graph

Status: built (steps 1-5 below) and checked on invented networks; step 6 (real data) is waiting for the data files,
which are not on the machine this was built on. First targets: London Victoria (VIC) and Clapham Junction (CLJ). Written so another
agent can pick it up cold. Read `README.md`, `plans/BETTER_LOOPS.md` (the diagnosis), then
`src/station_route_map.py` (`layout`, `assign_ordered`) and `src/hub_map.py` (`render`, `hard`, `ENGINES`).

## 1. Why the tree cannot be patched

The layout is a spanning tree rooted at the hub. A route step that is not a tree edge is either a chord the
bypass code pins as a parallel arm, or the route is sent the long way round the tree (out to the common
ancestor and back). The second case is the fold-back. It is kept whenever the route calls at a station on
the wrong arm, scores 2 against a route tolerance of 1, and so pushes the route to a later page.

At Clapham, Piccadilly and Leeds the cycles share stations, the bypass code plans one chord per cycle and
skips any chord whose arms touch an arm already used, and lane order comes from tree pre-order. Failures
grow with routes x cycles. "All routes on one page" is unreachable in that model by construction.

## 2. The method: blocks laid out as graphs, the rest as spokes

1. **Blocks.** Split the station graph (consecutive stops of the drawn routes) into 2-edge-connected
   blocks and bridges (`loops.blocks`). Bridges and the trees hanging off them are laid out as spokes,
   exactly as today.
2. **Skeleton.** Inside a block, merge every degree-2 chain into one *corridor* edge that carries its
   stations and routes. Junction stations (block degree != 2) and the hub are the skeleton nodes. A
   corridor may join a node to itself (a balloon loop) and two corridors may join the same two nodes.
3. **Embedding from geography.** At each skeleton node the cyclic order of corridor ends is their real
   compass order. The optimiser never chooses topology.
4. **Octilinear skeleton.** Each corridor end gets one of the 8 directions, in the node's cyclic order,
   with free directions reserved for spurs. A corridor is up to three straight segments (out, middle,
   in), so parallel arms leave and rejoin by diagonals. Segment lengths and node positions are variables
   of a linear program: every corridor closes on its two nodes (so every cycle closes), every corridor
   is at least its station count times the spacing, the hub sits at the origin. Pairs of segments that
   come out crossing or closer than their lane widths get a separation constraint on the side they are
   already on, and the LP is solved again (a few rounds). Direction choices start from the compass
   bearings; a small local search changes one node's assignment or one corridor's middle direction and
   keeps the layout with the fewest crossings, then bends, then deviation from the bearings.
5. **Stations** are spaced evenly along their corridor's polyline. Spurs off corridor stations leave to
   the side away from the nearest other corridor.
6. **Routes walk their real stations.** A fold-back is now only a real reversal (a route that calls at a
   station twice); it is drawn with a turn-back and is not a failure.
7. **Lanes.** On a corridor, routes are ordered by where they go after the far node (the signed turn at
   each junction ahead, lexicographically), then by where they came from before the near node. Routes
   that end inside the corridor sit on the left, earliest end first. Offsets are fixed per corridor and
   re-centred at junctions. A crossing inside a junction's radius is reported separately
   (`junction_crossings`) and counts less than one in the open.
8. **Engine.** `ENGINES` gets `("blocks", ...)`; `build_map_best` keeps whichever engine scores fewer
   hard failures, so no station gets worse. The block skeleton is laid out once for the full route set,
   so every page of a station can share one geometry (later).

## 3. Files

| file | holds |
|---|---|
| `src/skeleton.py` | station graph -> blocks -> corridors + junction nodes + rotation system (pure functions, no drawing) |
| `src/block_layout.py` | skeleton LP layout, station placement along corridors, spoke subtrees; returns the drawn graph |
| `src/block_render.py` | lanes on a graph, route geometry, dots, capsules, labels, rule report, SVG |
| `src/hub_map.py` | `LAYOUT` switch in `render`, the `blocks` engine, label and rule helpers shared with the tree renderer |
| `tests/test_blocks.py` | toy networks: diamond with onward line (Horsham), nested, balloon loop, two loops sharing a node, ladder |

## 4. Build order (each step has a check)

1. Skeleton extraction with tests (corridors found, bearings, balloon loop, parallel corridors).
2. LP layout of a skeleton with tests: diamond closes, arms are separated, no crossing; ladder solves.
3. Station placement + spokes; the toy maps draw with `retrace == 0`, `crossings == 0`.
4. Renderer on the drawn graph, rule report with the same keys as the tree renderer.
5. Engine switch; `pytest`; the tree engine's toy output must be byte-identical to before.
6. Real data: VIC and CLJ, all routes on one page. Compare `hard()` against the tree engine; look at the
   PNGs. Then Manchester Piccadilly, Leeds.

## 5. Results on invented networks (`tools/toy_station.py`)

Every grouped pattern on one page, spacing 80, `hard()` with the stub measure on. The blocks engine draws every
loop as a loop; what is left is a hub label that finds no free side and, on the 16-route network, one crossing
in the open and two inside junctions.

| network | routes | tree engine `hard()` | blocks engine `hard()` |
|---|---|---|---|
| horsham (split, two arms, merge, line beyond) | 4 | 0 | 0 |
| don (Doncaster-Sheffield via Rotherham or Meadowhall) | 5 | 12 (2 stubs) | 0 |
| vic (Victoria: Horsham via Crawley or Dorking) | 4 | 35 (2 fold-backs, 5 stubs, 1 crossing) | 0 |
| clj (Clapham: Kingston and Hounslow loops, Horsham, Sutton two ways) | 11 | 66 (6 fold-backs, 9 stubs) | 1 (hub label) |
| big (clj plus semi-fast variants and more branches) | 16 | 52 (4 fold-backs, 7 stubs, 2 crossings) | 3 (1 crossing, 2 at junctions, hub label) |
| clj, clean mode (the route picker) | 11 | 9 of 11 kept, hard 21 | 11 of 11 kept, hard 1 |

Time: about 12 s for the first layout of the 16-route network (the direction search), under 1 s for each further
spacing (directions are cached), 3 to 4 s for a clean-mode probe sequence.

## 6. Risks

- Separation by lazy constraints can make the LP infeasible; the fallback is a different direction set,
  and finally the old engine (kept by `build_map_best`).
- Spur subtrees off corridor stations can still collide with another corridor; the rule report shows it.
- Real reversals (Kingston loop) need the turn-back glyph; until then they count as `retrace`.
