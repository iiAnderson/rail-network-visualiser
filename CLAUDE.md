# Working in this repo

Read `README.md` first (pipeline, rules, inputs). Plans live in `plans/`; a plan's first lines give its status.

## Setup and data

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest
python -m pytest tests -q          # toy networks only, no data needed
```

Inputs are git-ignored and must be in `data/`: `national_patterns_days.csv`, `tiplocs.csv`,
`nr_schematic.json` (see README "Inputs"). Without them only the toy networks run.
Check `ls data/` before promising a real-station map.

## Layout engines

- `old` (default): spanning tree with patched loops (`src/station_route_map.py`, `src/hub_map.py`).
- `blocks`: cycles laid out as graphs, spokes elsewhere (`src/skeleton.py`, `src/block_layout.py`,
  `src/block_render.py`; design and results in `plans/BLOCK_LAYOUT.md`).

```bash
python src/hub_map.py VIC CLJ --engine blocks --mode all --patterns data/national_patterns_days.csv \
    --tiplocs data/tiplocs.csv --network data/nr_schematic.json
python src/generate_all.py --engine blocks --stations VIC CLJ --patterns ... --tiplocs ... --network ...
python tools/toy_station.py vic blocks      # invented network, no data; writes maps/toy/
```

Judge a map by its rule report (`retrace`, `stubs`, `crossings`, `junction_crossings`, `label_collision`,
`dot_on_line`) and by eye. `hub_map.hard()` is the score the route picker and the engine comparison use.

## Current state (2026-11-04)

The blocks engine is built and verified on toy networks only. Next step is `plans/BLOCK_LAYOUT.md`
step 6: run London Victoria and Clapham Junction on real data, compare with the old engine, then
Manchester Piccadilly and Leeds.

## Previews

`cairosvg` ignores `paint-order`, so the white label halo hides the text in a PNG; strip the
`stroke="#fff" ... paint-order="stroke"` attributes before converting (`tools/toy_station.py` does).

## Conventions

- Every rule is measured; a change to drawing must keep the tree engine's toy output identical
  (`tests/test_smoke.py`) unless the change is the point.
- Commit only when asked. Generated maps and anything derived from the route-map PDF stay out of git.
