-- 08_checkup_views.sql
-- 「浪费体检」标签的汇总视图
--
-- 背景：t_idle_label 里混了两种精度：
--   big_item  —— 逐笔人工确认，is_idle 是真值（0/1）
--   cat_prior —— 品类级先验，is_idle 统一写 0，真实信息在 note 里
--                （该品类"大约多少比例闲置"），**不能直接 SUM(is_idle) 当浪费金额**。
--
-- 所以算钱必须分两层求和再相加。本视图固化这个口径，避免应用层各写一套。
--
-- ⚠ 口径要点：品类级先验只作用于**大额阈值以下**的笔（体检页就是这么问的）。
--    大额笔已逐笔确认，再叠加品类先验会把同一笔算两遍
--    （实测差 ¥14,180.49，正好是大额池总额）。

USE spending_waste;

-- 视图 1：按层汇总（两行：big_item / cat_prior）
CREATE OR REPLACE VIEW v_checkup_summary AS
SELECT
  'big_item' AS layer,
  '逐笔人工确认' AS layer_desc,
  COUNT(*) AS n,
  SUM(l.is_idle) AS n_idle,
  ROUND(SUM(CASE WHEN l.is_idle = 1 THEN v.amount ELSE 0 END), 2) AS waste_amt,
  ROUND(SUM(v.amount), 2) AS pool_amt
FROM t_idle_label l
JOIN v_idle_candidate v ON v.trans_id = l.trans_id
WHERE l.sample_stratum = 'big_item'

UNION ALL

-- 品类级：先验比例从 note 里解析（'品类先验 ECOMM=10%（…）'）。
-- 比另建一列省事，且 note 是人能直接看懂的。
SELECT
  'cat_prior' AS layer,
  '品类先验推算' AS layer_desc,
  COUNT(*) AS n,
  NULL AS n_idle,
  ROUND(SUM(v.amount * CAST(REGEXP_SUBSTR(l.note, '[0-9]+') AS DECIMAL(5,2)) / 100), 2) AS waste_amt,
  ROUND(SUM(v.amount), 2) AS pool_amt
FROM t_idle_label l
JOIN v_idle_candidate v ON v.trans_id = l.trans_id
WHERE l.sample_stratum = 'cat_prior';

-- 视图 2：总计——一句话回答"这份账单里多少钱是浪费"
CREATE OR REPLACE VIEW v_checkup_total AS
SELECT
  pool.amt                                                   AS pool_amt,
  total.waste_amt                                            AS waste_amt,
  ROUND(pool.amt - total.waste_amt, 2)                       AS kept_amt,
  ROUND(total.waste_amt / NULLIF(pool.amt, 0) * 100, 2)      AS waste_rate_pct,
  (SELECT COUNT(*) FROM t_idle_label WHERE sample_stratum = 'big_item')  AS big_n,
  (SELECT COUNT(*) FROM t_idle_label WHERE sample_stratum = 'cat_prior') AS cat_n
FROM
  (SELECT SUM(amount) AS amt FROM v_idle_candidate) AS pool,
  (SELECT SUM(waste_amt) AS waste_amt FROM v_checkup_summary) AS total;
