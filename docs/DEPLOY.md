# Publishing the station route maps

The maps are served at **<https://rail.robbiea.co.uk/routes>**, one of the visualisations on the rail site.
This document is the whole process: what lives where, how to regenerate, how to publish, and how to check.

## What lives where

The site belongs to [uk-rail-open-data](https://github.com/iiAnderson/uk-rail-open-data): its Terraform owns
the S3 bucket (`uk-rail-dashboard`) and the CloudFront distribution, and its `deploy.sh` publishes the other
pages and `rail.js`, the navigation rail every page shares. This repository owns exactly two things on it:

| Path | What | Published by |
|---|---|---|
| `/routes.html` (served at `/routes`) | the station viewer, `web/index.html` with the site's head | `tools/site.sh publish` |
| `/routes/*` | `stations.js`, `manifest.json` and one SVG per station page | `tools/site.sh publish` |
| `/rail.js` | the navigation rail, including the "Network diagrams" entry | uk-rail-open-data `deploy.sh` |

The two deploys never touch each other's files: `tools/site.sh` only writes `routes.html` and syncs only under
`routes/`, and uk-rail-open-data's `deploy.sh` excludes both (as it excludes `data/`, which its Lambdas own).
**Keep that exclusion.** Its sync runs with `--delete`, so without it the next deploy of the rest of the site
removes every map.

The CloudFront function rewrites `/routes` to `/routes.html`, as it does for `/tracks`; the page loads its maps
from `routes/` beside it and links to a station with `#CRS` (`/routes#CLJ`).

The site is also listed on the robbiea.co.uk landing page ([robbiea-home](https://github.com/iiAnderson),
private): an entry in the Visualisations list in `site/index.html` and one in `site/activity.json`.

## The process

### After a change to the engine or the data

1. **Measure** on the reference stations, against the last good run:

   ```bash
   python tools/compare_engines.py --label my-change --against <last-label>
   ```

   Look at the maps in `maps/compare/my-change/` as well as the numbers (see CLAUDE.md).

2. **Rebuild every station:**

   ```bash
   tools/site.sh build
   ```

   About 15 minutes for ~1,300 stations on 6 workers. It builds into `maps_single.new/` and swaps it into
   `maps_single/` only if every station drew and the set is complete; the previous set is kept in
   `maps_single.prev/` and the run log in `maps_single.log`. Review locally:
   `open maps_single/index.html#CLJ`.

3. **Publish:**

   ```bash
   tools/site.sh publish --dry-run   # what would be uploaded and deleted
   tools/site.sh publish
   ```

   It checks the set again (every map in the manifest exists, at least 1,000 stations), uploads the maps, then
   the page, then invalidates `/routes`, `/routes.html` and `/routes/*` and waits for the invalidation. The
   change is live when it returns.

`tools/site.sh release` does steps 2 and 3 in one go, for when you have already reviewed the change.

### After a change to the viewer only (`web/index.html`)

`tools/site.sh publish` alone: the page is rebuilt from `web/index.html` on every publish, and the maps are
re-uploaded only where they changed. Refresh the local copy with
`python -c "import sys; sys.path.insert(0, 'src'); import generate_all; generate_all.write_viewer('maps_single')"`.

The viewer is one file for both uses. `__MAPS_BASE__` is where it finds the maps (empty beside them locally,
`routes/` on the site) and `<!--site-head-->` is where publishing inserts the site's fonts, theme and
`rail.js`. Without that head it looks and behaves exactly as it does locally.

### Adding or renaming the entry in the site navigation

That is uk-rail-open-data's `site/rail.js` (the `ITEMS` list), published by its `deploy.sh`.

Every page loads it as `rail.js?v=<first 10 hex digits of its SHA-1>`: the file carries no Cache-Control, so
without a new URL a browser that has seen the old rail keeps it for days or weeks. uk-rail-open-data's
`deploy.sh` fills the version into its pages, and `tools/site.sh publish` into `/routes`, from the same file —
so after changing `rail.js`, deploy that repository **and** run `tools/site.sh publish` here, or `/routes` keeps
loading the old version.

## Requirements

- The inputs in `data/` or `../rail-network-visualiser-data/` (`DATA_DIR` to point elsewhere): see README.
- `.venv` set up as in CLAUDE.md.
- AWS credentials that can write the `uk-rail-dashboard` bucket and invalidate the distribution, and a checkout
  of uk-rail-open-data at `../uk-rail-open-data` with its Terraform state (`RAIL_SITE_REPO` to point elsewhere),
  from which the bucket and distribution are read. `SITE_BUCKET` and `DISTRIBUTION_ID` skip that lookup.

## Checking a publish

```bash
curl -sI https://rail.robbiea.co.uk/routes | head -1                  # 200
curl -s https://rail.robbiea.co.uk/routes/manifest.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
curl -sI https://rail.robbiea.co.uk/routes/CLJ.svg | grep -i content-type   # image/svg+xml
```

## Rolling back

`maps_single.prev/` is the set before the last build: `MAPS=maps_single.prev tools/site.sh publish`. The bucket
is versioned as well, so any earlier object can be restored from S3.
