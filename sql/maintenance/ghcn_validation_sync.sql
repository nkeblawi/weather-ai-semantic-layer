-- Push NOAA's revisions of GHCN values already loaded down to the tables the app reads.
-- A revision (a corrected value, or a QC flag NOAA added later) reaches cleaned.obs_ghcn_conv,
-- but merge_obs never replaces a GHCN row with another GHCN row, so it stops there.
-- Revised = a GHCN row in obs_merged whose value or flags differ from obs_ghcn_conv. Only those
-- rows, and the station-dates they're on, are touched; everything else is left as is.
-- obs_ghcn_conv already has convert_ghcn.py's QC rule and the qc_allowlist applied.
-- Target: prod (weather). Idempotent: every step touches only rows that still differ, so a
-- clean rerun reports 0 affected rows in all three steps. Run the statements in order.

-- 0. Preview (changes nothing): the revisions step 1 would apply
SELECT m.station_id, m.obs_date, m.variable,
       m.value AS old_value, c.value AS new_value, m.qflag AS old_qflag, c.qflag AS new_qflag
FROM weather.cleaned.obs_merged m
JOIN weather.cleaned.obs_ghcn_conv c
  ON c.station_id = m.station_id AND c.obs_date = m.obs_date AND c.variable = m.variable
WHERE m.source = 'GHCN'
  AND NOT (m.value <=> c.value AND m.mflag <=> c.mflag AND m.qflag <=> c.qflag AND m.sflag <=> c.sflag)
ORDER BY m.obs_date, m.station_id, m.variable;

-- 1. cleaned.obs_merged: take the revised GHCN rows from obs_ghcn_conv
MERGE INTO weather.cleaned.obs_merged t
USING weather.cleaned.obs_ghcn_conv s
ON t.station_id = s.station_id AND t.obs_date = s.obs_date AND t.variable = s.variable
WHEN MATCHED AND t.source = 'GHCN'
  AND NOT (t.value <=> s.value AND t.mflag <=> s.mflag AND t.qflag <=> s.qflag AND t.sflag <=> s.sflag)
THEN UPDATE SET
  value = s.value, mflag = s.mflag, qflag = s.qflag, sflag = s.sflag, ingested_at = s.ingested_at;

-- 2. cleaned.obs_pivot: re-pivot the station-dates where a GHCN value in obs_merged
--    differs from obs_pivot (the rows step 1 revised)
MERGE INTO weather.cleaned.obs_pivot t
USING (
  SELECT m.station_id, m.obs_date,
         max(CASE WHEN m.variable = 'AWND' THEN m.value END) AS AWND,
         max(CASE WHEN m.variable = 'PRCP' THEN m.value END) AS PRCP,
         max(CASE WHEN m.variable = 'SNOW' THEN m.value END) AS SNOW,
         max(CASE WHEN m.variable = 'SNWD' THEN m.value END) AS SNWD,
         max(CASE WHEN m.variable = 'TMAX' THEN m.value END) AS TMAX,
         max(CASE WHEN m.variable = 'TMIN' THEN m.value END) AS TMIN
  FROM weather.cleaned.obs_merged m
  JOIN (
    SELECT DISTINCT m.station_id, m.obs_date
    FROM weather.cleaned.obs_merged m
    JOIN weather.cleaned.obs_pivot p ON p.station_id = m.station_id AND p.obs_date = m.obs_date
    WHERE m.source = 'GHCN'
      AND NOT (m.value <=> CASE m.variable
        WHEN 'AWND' THEN p.AWND WHEN 'PRCP' THEN p.PRCP WHEN 'SNOW' THEN p.SNOW
        WHEN 'SNWD' THEN p.SNWD WHEN 'TMAX' THEN p.TMAX WHEN 'TMIN' THEN p.TMIN END)
  ) d ON d.station_id = m.station_id AND d.obs_date = m.obs_date
  GROUP BY m.station_id, m.obs_date
) s
ON t.station_id = s.station_id AND t.obs_date = s.obs_date
WHEN MATCHED AND NOT (
  t.AWND <=> s.AWND AND t.PRCP <=> s.PRCP AND t.SNOW <=> s.SNOW AND
  t.SNWD <=> s.SNWD AND t.TMAX <=> s.TMAX AND t.TMIN <=> s.TMIN
) THEN UPDATE SET
  AWND = s.AWND, PRCP = s.PRCP, SNOW = s.SNOW, SNWD = s.SNWD, TMAX = s.TMAX, TMIN = s.TMIN;

-- 3. analytics.observations: copy the re-pivoted GHCN station-dates and recompute derived vars
--    (same formulas as derived_vars.py; HDD/CDD are NULL when TAVG is NULL)
MERGE INTO weather.analytics.observations t
USING (
  SELECT station_id, obs_date, AWND, PRCP, SNOW, SNWD, TMAX, TMIN, (TMAX + TMIN) / 2.0 AS TAVG
  FROM weather.cleaned.obs_pivot
  WHERE source = 'GHCN'
) s
ON t.station_id = s.station_id AND t.obs_date = s.obs_date
-- only rows that differ from obs_pivot (TAVG/HDD/CDD follow from TMAX/TMIN)
WHEN MATCHED AND NOT (
  t.AWND <=> s.AWND AND t.PRCP <=> s.PRCP AND t.SNOW <=> s.SNOW AND
  t.SNWD <=> s.SNWD AND t.TMAX <=> s.TMAX AND t.TMIN <=> s.TMIN
) THEN UPDATE SET
  AWND = s.AWND, PRCP = s.PRCP, SNOW = s.SNOW, SNWD = s.SNWD,
  TMAX = s.TMAX, TMIN = s.TMIN, TAVG = s.TAVG,
  HDD = CAST(round(CASE WHEN s.TAVG IS NULL THEN NULL WHEN s.TAVG < 65 THEN 65 - s.TAVG ELSE 0.0 END) AS INT),
  CDD = CAST(round(CASE WHEN s.TAVG IS NULL THEN NULL WHEN s.TAVG > 65 THEN s.TAVG - 65 ELSE 0.0 END) AS INT);
