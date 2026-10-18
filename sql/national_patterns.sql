-- Calling pattern of every passenger service that ran on the chosen days (one row per service).
-- Replace {{YEAR}}, {{MONTH}} and {{DAYS}} (e.g. '08','09','10') before running. Use typical
-- midweek days from a month with no major disruption. normalised_v1 is partitioned by service date.
SELECT service_date, rid, toc, train_id, origin_tpl, destination_tpl,
  array_join(transform(filter(stops, s -> s.arr_sched IS NOT NULL OR s.dep_sched IS NOT NULL), s -> s.tpl), ',') AS pattern,
  array_join(transform(filter(stops, s -> s.arr_sched IS NOT NULL OR s.dep_sched IS NOT NULL), s -> coalesce(s.dep_sched, s.arr_sched)), ',') AS times
FROM normalised_v1
WHERE year = '{{YEAR}}' AND month = '{{MONTH}}' AND day IN ({{DAYS}})
  AND passenger = true AND cancellation_status = 'ran'
