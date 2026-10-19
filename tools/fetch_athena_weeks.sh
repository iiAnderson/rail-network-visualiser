#!/bin/bash
# Fetch the grouped pattern file for every Tuesday-Thursday between two dates (all in one year), plus tiplocs.
#
#   ATHENA_DB=my_database ATHENA_OUTPUT=s3://my-bucket/athena-results/ \
#     tools/fetch_athena_weeks.sh 2026-06-02 2026-10-15
#
# Writes data/national_patterns_days.csv and data/tiplocs.csv. Scan cost is roughly one partition per day.
set -euo pipefail
FROM=${1:?start date YYYY-MM-DD}; TO=${2:?end date YYYY-MM-DD}
DB=${ATHENA_DB:?set ATHENA_DB}; OUT=${ATHENA_OUTPUT:?set ATHENA_OUTPUT}
cd "$(dirname "$0")/.."; mkdir -p data
read -r YEAR PARTS <<<"$(python3 - "$FROM" "$TO" <<'PY'
import sys, datetime, collections
a, b = (datetime.date.fromisoformat(x) for x in sys.argv[1:3])
assert a.year == b.year, "keep the range inside one year"
by = collections.defaultdict(list)
d = a
while d <= b:
    if d.weekday() in (1, 2, 3):
        by[d.month].append("'%02d'" % d.day)
    d += datetime.timedelta(days=1)
print(a.year, " OR ".join("(month = '%02d' AND day IN (%s))" % (m, ",".join(v)) for m, v in sorted(by.items())))
PY
)"
run() {  # run <sql> <dest.csv>
  local qid state
  qid=$(aws athena start-query-execution --query-string "$1" \
        --query-execution-context Database="$DB" --result-configuration OutputLocation="$OUT" \
        --output text --query QueryExecutionId)
  while true; do
    state=$(aws athena get-query-execution --query-execution-id "$qid" \
            --query 'QueryExecution.Status.State' --output text)
    case $state in SUCCEEDED) break;; FAILED|CANCELLED) echo "query $qid $state" >&2; exit 1;; esac
    sleep 2
  done
  aws s3 cp "${OUT%/}/$qid.csv" "$2" --quiet
}
SQL=$(python3 - "$YEAR" "$PARTS" <<'PY'
import sys
t = open("sql/national_patterns_grouped.sql").read()
print(t.replace("{{YEAR}}", sys.argv[1]).replace("{{PARTITIONS}}", sys.argv[2]))
PY
)
run "$SQL" data/national_patterns_days.csv
run "$(cat sql/tiplocs.sql)" data/tiplocs.csv
echo "wrote data/national_patterns_days.csv and data/tiplocs.csv"
