-- One row per distinct timetabled train (operator + calling pattern + times), with the list of days it
-- ran. Use many typical days (every Tue-Thu over a few months works well): station_route_map.Data then
-- builds a "normal day" from the median across days, so engineering works on a few days (a closed main
-- line, a diversion) do not show up as the normal service.
-- Replace {{PARTITIONS}} with e.g.  (month = '09' AND day IN ('08','09','10')) OR (month = '10' AND day IN ('06'))
-- and {{YEAR}}. normalised_v1 is partitioned by service date, so only those days are scanned.
SELECT toc, pattern, times, array_join(array_sort(array_agg(service_date)), ',') AS days
FROM (
  SELECT service_date, toc,
    array_join(transform(filter(stops, s -> s.arr_sched IS NOT NULL OR s.dep_sched IS NOT NULL), s -> s.tpl), ',') AS pattern,
    array_join(transform(filter(stops, s -> s.arr_sched IS NOT NULL OR s.dep_sched IS NOT NULL), s -> coalesce(s.dep_sched, s.arr_sched)), ',') AS times
  FROM normalised_v1
  WHERE year = '{{YEAR}}' AND ({{PARTITIONS}})
    AND passenger = true AND cancellation_status = 'ran'
)
GROUP BY toc, pattern, times
