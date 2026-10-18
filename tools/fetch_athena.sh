#!/bin/bash
# Fetch the two input CSVs from AWS Athena.
#
#   ATHENA_DB=my_database ATHENA_OUTPUT=s3://my-bucket/athena-results/ \
#     tools/fetch_athena.sh 2026 09 "'08','09','10','15','16','17','22','23','24'"
#
# Needs the aws CLI configured. Writes data/national_patterns.csv and data/tiplocs.csv.
set -euo pipefail
YEAR=${1:?year, e.g. 2026}; MONTH=${2:?month, e.g. 09}; DAYS=${3:?quoted days, e.g. "'08','09'"}
DB=${ATHENA_DB:?set ATHENA_DB}; OUT=${ATHENA_OUTPUT:?set ATHENA_OUTPUT}
cd "$(dirname "$0")/.."; mkdir -p data

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

SQL=$(sed -e "s/{{YEAR}}/$YEAR/" -e "s/{{MONTH}}/$MONTH/" -e "s/{{DAYS}}/${DAYS//\//\\/}/" sql/national_patterns.sql)
run "$SQL" data/national_patterns.csv
run "$(cat sql/tiplocs.sql)" data/tiplocs.csv
echo "wrote data/national_patterns.csv and data/tiplocs.csv"
