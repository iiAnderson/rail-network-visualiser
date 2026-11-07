#!/usr/bin/env bash
#
# Build the station route maps and publish them to rail.robbiea.co.uk/routes.
#
#   tools/site.sh build      regenerate every station into maps_single/ (blocks engine, one page each)
#   tools/site.sh publish    upload maps_single/ to the site and invalidate the cache
#   tools/site.sh release    build, then publish
#
#   tools/site.sh publish --dry-run     show what would be uploaded or deleted, change nothing
#
# The site itself (bucket, CloudFront, the navigation rail) belongs to the uk-rail-open-data repository. This
# repository owns exactly two things on it: the page /routes.html and everything under /routes/. That repo's
# deploy.sh excludes both, so neither deploy can overwrite or delete the other's files. See docs/DEPLOY.md.
#
# Environment (all optional):
#   DATA_DIR          the inputs (default: data/, else ../rail-network-visualiser-data)
#   RAIL_SITE_REPO    the uk-rail-open-data checkout, for its Terraform outputs (default: ../uk-rail-open-data)
#   SITE_BUCKET, DISTRIBUTION_ID   skip the Terraform lookup
#   MAPS              the folder to build into / publish from (default: maps_single)
#   WORKERS           parallel stations while building (default: CPUs - 2)

set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

MAPS="${MAPS:-maps_single}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
MIN_MAPS=1000          # refuse to publish a set this small: it is a failed or partial build, and --delete would
                       # take every missing station off the site

data_dir() {
  if [ -n "${DATA_DIR:-}" ]; then echo "$DATA_DIR"
  elif [ -f data/national_patterns_days.csv ]; then echo data
  else echo ../rail-network-visualiser-data; fi
}

build() {
  local D; D="$(data_dir)"
  for f in national_patterns_days.csv tiplocs.csv nr_schematic.json; do
    [ -f "$D/$f" ] || { echo "error: $D/$f is missing (set DATA_DIR)" >&2; exit 1; }
  done
  # Build into a fresh folder and swap it in only if the run succeeds, so a station that no longer qualifies
  # does not linger as a stale map and a failed run leaves the last good set in place.
  local NEW="$MAPS.new"
  rm -rf "$NEW"
  "$PY" src/generate_all.py --engine blocks --single --top 5000 \
      --patterns "$D/national_patterns_days.csv" --tiplocs "$D/tiplocs.csv" --network "$D/nr_schematic.json" \
      ${WORKERS:+--workers "$WORKERS"} --out "$NEW" | tee "$MAPS.log"
  check "$NEW"
  rm -rf "$MAPS.prev"
  [ -d "$MAPS" ] && mv "$MAPS" "$MAPS.prev"
  mv "$NEW" "$MAPS"
  echo "built $MAPS/ (previous set kept in $MAPS.prev/)"
}

check() {
  local dir="$1"
  [ -f "$dir/manifest.json" ] && [ -f "$dir/stations.js" ] || { echo "error: $dir has no manifest/stations.js" >&2; exit 1; }
  "$PY" - "$dir" "$MIN_MAPS" <<'EOF'
import json, os, sys
d, need = sys.argv[1], int(sys.argv[2])
m = json.load(open(os.path.join(d, "manifest.json")))
missing = [c for c, r in m.items() for k in range(1, (r.get("pages") or 1) + 1)
           if not os.path.exists(os.path.join(d, c + ("" if k == 1 else "_%d" % k) + ".svg"))]
if missing:
    sys.exit("error: %d maps in the manifest have no SVG, e.g. %s" % (len(missing), missing[:5]))
if len(m) < need:
    sys.exit("error: only %d stations (expected at least %d): refusing to publish a partial set" % (len(m), need))
print("checked %s: %d stations, every map present" % (d, len(m)))
EOF
}

publish() {
  local DRY=""; [ "${1:-}" = "--dry-run" ] && DRY="--dryrun"
  local SITE_REPO="${RAIL_SITE_REPO:-../uk-rail-open-data}"
  local BUCKET="${SITE_BUCKET:-$(terraform -chdir="$SITE_REPO/terraform" output -raw bucket_name)}"
  local DISTRIBUTION="${DISTRIBUTION_ID:-$(terraform -chdir="$SITE_REPO/terraform" output -raw distribution_id)}"
  check "$MAPS"

  # The page: the same viewer as maps_single/index.html, pointed at routes/ and given the site's head (fonts,
  # theme before first paint, the shared navigation rail). rail.js lives at the site root, deployed by
  # uk-rail-open-data; its ITEMS list carries the "Network diagrams" entry.
  STAGE="$(mktemp -d)"          # global: the EXIT trap runs after this function has returned
  trap 'rm -rf "${STAGE:-}"' EXIT
  # rail.js is loaded as rail.js?v=<its hash>, as uk-rail-open-data's pages load it, so a changed rail is a new URL
  # and browsers fetch it rather than keeping a stale copy
  local RAIL_V; RAIL_V="$(shasum "$SITE_REPO/site/rail.js" | cut -c1-10)"
  "$PY" - "$STAGE" "$RAIL_V" <<'EOF'
import sys
sys.path.insert(0, "src")
import generate_all
head = """<meta name="description" content="Every service pattern from every station on the British network, drawn as a route map.">
<link rel="canonical" href="https://rail.robbiea.co.uk/routes">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<script>
/* Set before first paint, as on every page of the site; rail.js owns the toggle. */
try{var t=localStorage.getItem('site-theme')||localStorage.getItem('tracks-theme');
    document.documentElement.dataset.theme=t==='light'?'light':'dark';}catch(e){}
</script>
<script src="rail.js?v=%s" defer></script>""" % sys.argv[2]
generate_all.write_viewer(sys.argv[1], maps_base="routes/", site_head=head)
EOF
  mv "$STAGE/index.html" "$STAGE/routes.html"
  perl -pi -e 's{<title>.*?</title>}{<title>Network diagrams — every service pattern from every British station</title>}' "$STAGE/routes.html"

  # Maps first, page second: a reader who loads the new page during the upload still finds every map it names.
  # --delete is scoped to routes/, so it can only remove maps of stations that dropped out of the set.
  aws s3 sync "$MAPS/" "s3://$BUCKET/routes/" --delete $DRY \
      --exclude 'index.html' --exclude '*.log' --exclude '.*'
  aws s3 cp "$STAGE/routes.html" "s3://$BUCKET/routes.html" --content-type 'text/html; charset=utf-8' $DRY
  if [ -n "$DRY" ]; then echo "dry run: nothing uploaded, nothing invalidated"; return; fi
  echo "published to s3://$BUCKET/routes.html and routes/"

  # Everything outside /data/ carries an hour-long TTL; without this a publish is invisible for up to an hour.
  # /routes/* is one path however many maps it covers (the first 1,000 paths a month are free).
  if [ -n "$DISTRIBUTION" ]; then
    local ID
    ID=$(aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION" \
           --paths '/routes' '/routes.html' '/routes/*' --query 'Invalidation.Id' --output text)
    echo "invalidating $DISTRIBUTION ($ID)"
    aws cloudfront wait invalidation-completed --distribution-id "$DISTRIBUTION" --id "$ID"
    echo "invalidation complete: https://rail.robbiea.co.uk/routes"
  fi
}

case "${1:-}" in
  build)   build ;;
  publish) shift; publish "$@" ;;
  release) build; publish ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
