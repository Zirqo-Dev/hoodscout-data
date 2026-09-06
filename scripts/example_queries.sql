-- Example queries against hoodscout.db (build with: python3 scripts/build_db.py)
--
-- Timestamps are ISO8601 with microseconds and a +00:00 offset. SQLite's
-- julianday() parses that directly, so windows are expressed in days
-- (2.0/24 = two hours) rather than by counting rows: snapshot spacing is not
-- uniform, and a run that fails or a token that drops out of the feed leaves
-- gaps that a fixed row count would silently misread.


-- 1. Persistence: tokens that were BEST in at least 5 of their last 8 snapshots.
--
-- meme_snapshots.tier is only populated for rows sourced from a full snapshot
-- (data/latest.json and data/daily/*.json); plain history.jsonl rows carry no
-- tier and are excluded here rather than counted as "not BEST".
WITH ranked AS (
    SELECT ca, ts, tier,
           ROW_NUMBER() OVER (PARTITION BY ca ORDER BY ts DESC) AS rn
    FROM meme_snapshots
    WHERE tier IS NOT NULL
)
SELECT t.symbol,
       r.ca,
       SUM(CASE WHEN r.tier = 'BEST' THEN 1 ELSE 0 END) AS best_count,
       COUNT(*)                                         AS scored_snapshots,
       MAX(r.ts)                                        AS last_seen
FROM ranked r
JOIN tokens t ON t.ca = r.ca
WHERE r.rn <= 8
GROUP BY r.ca, t.symbol
HAVING best_count >= 5
ORDER BY best_count DESC, t.symbol;


-- 2. Buyer-rate acceleration: mean byr24 over the last ~2h against the
--    trailing ~2-6h, per token, anchored to each token's own latest snapshot
--    so a token that stopped reporting is not compared against wall-clock now.
WITH anchor AS (
    SELECT ca, MAX(ts) AS latest_ts
    FROM meme_snapshots
    WHERE byr24 IS NOT NULL
    GROUP BY ca
),
windows AS (
    SELECT m.ca,
           AVG(CASE WHEN julianday(a.latest_ts) - julianday(m.ts) <= 2.0/24
                    THEN m.byr24 END) AS byr_recent_2h,
           AVG(CASE WHEN julianday(a.latest_ts) - julianday(m.ts) >  2.0/24
                     AND julianday(a.latest_ts) - julianday(m.ts) <= 6.0/24
                    THEN m.byr24 END) AS byr_trailing_6h,
           COUNT(*)                   AS rows_in_window
    FROM meme_snapshots m
    JOIN anchor a ON a.ca = m.ca
    WHERE m.byr24 IS NOT NULL
      AND julianday(a.latest_ts) - julianday(m.ts) <= 6.0/24
    GROUP BY m.ca
)
SELECT t.symbol,
       ROUND(w.byr_recent_2h, 1)   AS byr_2h,
       ROUND(w.byr_trailing_6h, 1) AS byr_2_6h,
       ROUND(100.0 * (w.byr_recent_2h / w.byr_trailing_6h - 1), 1) AS accel_pct,
       w.rows_in_window
FROM windows w
JOIN tokens t ON t.ca = w.ca
WHERE w.byr_recent_2h IS NOT NULL
  AND w.byr_trailing_6h > 0
ORDER BY accel_pct DESC
LIMIT 15;


-- 3. Stock token premium trajectory: the whole drift for HIMS and AMC in one
--    pass, instead of reading 90 rows of stocks.jsonl by eye.
SELECT symbol,
       ts,
       ROUND(premium_pct, 2)    AS premium_pct,
       ROUND(price_usd, 4)      AS price_usd,
       ROUND(locked_pct_est, 2) AS locked_pct
FROM stock_snapshots
WHERE symbol IN ('HIMS', 'AMC')
  AND premium_pct IS NOT NULL
ORDER BY symbol, ts;
