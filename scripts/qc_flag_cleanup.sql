-- Apply the convert_ghcn.py QC rule to rows already loaded.
-- Rule: NULL any GHCN value with a QFLAG, except keys in weather.cleaned.qc_allowlist
-- (seeded by the "Create AllowList Table" saved query in Databricks). Rows and qflags are kept.
-- New rows get the rule from convert_ghcn.py; merge_obs never rewrites an existing
-- GHCN row, so existing data needs this patch. Run the statements in order.
-- Target: prod (weather). Idempotent: every step touches only rows that still need the fix,
-- so a clean rerun reports 0 affected rows in all four steps.

-- 1. cleaned.obs_ghcn_conv
UPDATE weather.cleaned.obs_ghcn_conv c
SET value = NULL
WHERE qflag IS NOT NULL AND value IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM weather.cleaned.qc_allowlist a
                  WHERE a.station_id = c.station_id AND a.obs_date = c.obs_date
                    AND a.variable = c.variable);

-- 2. cleaned.obs_merged (GHCN rows only; ACIS rows carry no qflag)
UPDATE weather.cleaned.obs_merged m
SET value = NULL
WHERE source = 'GHCN' AND qflag IS NOT NULL AND value IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM weather.cleaned.qc_allowlist a
                  WHERE a.station_id = m.station_id AND a.obs_date = m.obs_date
                    AND a.variable = m.variable);

-- 3. cleaned.obs_pivot: NULL the flagged variables on each affected station-date
MERGE INTO weather.cleaned.obs_pivot t
USING (
  SELECT station_id, obs_date,
         max(variable = 'AWND') f_awnd, max(variable = 'PRCP') f_prcp,
         max(variable = 'SNOW') f_snow, max(variable = 'SNWD') f_snwd,
         max(variable = 'TMAX') f_tmax, max(variable = 'TMIN') f_tmin
  FROM weather.cleaned.obs_merged
  WHERE source = 'GHCN' AND qflag IS NOT NULL AND value IS NULL
  GROUP BY station_id, obs_date
) f
ON t.station_id = f.station_id AND t.obs_date = f.obs_date
-- only rows where a flagged variable still has a value
WHEN MATCHED AND (
  (f.f_awnd AND t.AWND IS NOT NULL) OR (f.f_prcp AND t.PRCP IS NOT NULL) OR
  (f.f_snow AND t.SNOW IS NOT NULL) OR (f.f_snwd AND t.SNWD IS NOT NULL) OR
  (f.f_tmax AND t.TMAX IS NOT NULL) OR (f.f_tmin AND t.TMIN IS NOT NULL)
) THEN UPDATE SET
  AWND = IF(f.f_awnd, NULL, t.AWND),
  PRCP = IF(f.f_prcp, NULL, t.PRCP),
  SNOW = IF(f.f_snow, NULL, t.SNOW),
  SNWD = IF(f.f_snwd, NULL, t.SNWD),
  TMAX = IF(f.f_tmax, NULL, t.TMAX),
  TMIN = IF(f.f_tmin, NULL, t.TMIN);

-- 4. analytics.observations: copy the patched columns and recompute derived vars
--    (same formulas as derived_vars.py; HDD/CDD are NULL when TAVG is NULL)
MERGE INTO weather.analytics.observations t
USING (
  SELECT p.station_id, p.obs_date, p.AWND, p.PRCP, p.SNOW, p.SNWD, p.TMAX, p.TMIN,
         (p.TMAX + p.TMIN) / 2.0 AS TAVG
  FROM weather.cleaned.obs_pivot p
  JOIN (SELECT DISTINCT station_id, obs_date FROM weather.cleaned.obs_merged
        WHERE source = 'GHCN' AND qflag IS NOT NULL AND value IS NULL) f
    ON p.station_id = f.station_id AND p.obs_date = f.obs_date
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
