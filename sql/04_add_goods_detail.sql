-- =============================================================
--  增量迁移：新增商品名补全表 t_goods_detail，并把商品名挂进候选视图
--  日期：2026-10-14
--
--  为什么不重跑 01_schema.sql
--  --------------------------
--  01_schema.sql 开头是 DROP DATABASE IF EXISTS，重跑会把 3783 条
--  交易和全部标注一起清掉。已上线的库只能走增量脚本。
--  本脚本可重复执行（CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE VIEW）。
--  ⚠ 若库里已存在**旧结构**的 t_goods_detail（缺 alt_titles / spec 列），
--    MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS，需先手工 `DROP TABLE t_goods_detail;`
--    再跑本脚本，最后重新执行 sql/05_goods_detail.sql 灌数据。
--
--  背景：账单在电商这笔上不记商品名（拼多多只记到"拼多多平台商户/先用后付"），
--        导致 776 条标注候选里 523 条的商品说明是占位文本，标注时回忆不起来。
-- =============================================================

USE spending_waste;
SET NAMES utf8mb4;


-- 1) 商品名补全表（派生表，可由拼多多订单列表重建）
CREATE TABLE IF NOT EXISTS t_goods_detail (
  trans_id         BIGINT UNSIGNED NOT NULL COMMENT '对应交易（一交易一补全行）',
  shop_name        VARCHAR(120) DEFAULT NULL    COMMENT '店铺名，如"六品堂官方旗舰店"',
  goods_title      VARCHAR(255) DEFAULT NULL    COMMENT '商品标题，如"六品堂宋徽宗瘦金体字帖…"',
  spec             VARCHAR(120) DEFAULT NULL    COMMENT '规格，如"×1，瘦金体一本通"',
  alt_titles       VARCHAR(500) DEFAULT NULL    COMMENT '匹配没定夺时的候选商品标题，用 / 分隔，供人工判断',
  match_confidence ENUM('unique','same_cat','ordered','fuzzy')
                                NOT NULL DEFAULT 'fuzzy' COMMENT '匹配强度：唯一/同类/顺序对齐/跨类',
  source           VARCHAR(20)  NOT NULL DEFAULT 'pdd_order' COMMENT '补全来源',
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (trans_id),
  KEY idx_goods_confidence (match_confidence),
  CONSTRAINT fk_goods_trans FOREIGN KEY (trans_id)
    REFERENCES t_transaction (trans_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品名补全表（派生，可由订单列表重建）';


-- 2) 候选视图带上补全商品名
--    仍是 1:0或1 的 LEFT JOIN（t_goods_detail 主键就是 trans_id），
--    候选行数、口径、抽样层都不变 —— 改完必须复验 776 / 2516 / 83094.45。
CREATE OR REPLACE VIEW v_idle_candidate AS
SELECT
  t.trans_id,
  t.user_id,
  t.trade_time,
  t.counterparty,
  t.product_desc,
  g.shop_name,
  g.goods_title,
  g.spec,
  g.alt_titles,
  g.match_confidence                            AS goods_match,
  t.amount,
  c.category_id,
  c.category_code,
  c.category_name,
  CASE
    WHEN t.amount >= 20 THEN 'all'
    WHEN t.amount >= 10 THEN 's3_10_20'
    WHEN t.amount >= 5  THEN 's2_5_10'
    ELSE                     's1_0_5'
  END AS stratum
FROM t_transaction t
JOIN t_category c ON c.category_id = t.category_id
LEFT JOIN t_goods_detail g ON g.trans_id = t.trans_id
WHERE t.direction = 'out'
  AND t.is_refund = 0
  AND t.is_primary = 1
  AND c.is_consumption = 1
  AND c.is_durable = 1
  AND (t.trade_status IS NULL OR t.trade_status NOT IN ('已全额退款', '交易关闭'));
