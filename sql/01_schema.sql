-- =============================================================
--  大学生闲置消费识别与预警系统  数据库结构脚本
--  数据库：spending_waste     字符集：utf8mb4
--  版本：v1.0
--  说明：本脚本只负责"结构"（表 / 视图 / 索引），种子数据见 02_seed.sql
--        脚本可重复执行，会先 DROP 再 CREATE
--  设计要点：
--    1. 事实表(t_transaction) 与 特征表(t_trans_feature) 分离 —— 特征可重算，不动原始数据
--    2. 闲置标注(t_idle_label) 独立成表 —— 它是模型标签，也是本项目无法被复制的核心资产
--    3. 预警记录(t_risk_record) 带事后回填字段 —— 预测可被后续真实行为验证，形成闭环
--    4. 统计口径统一收敛到视图 —— 应用层不重复写 WHERE 条件，避免口径漂移
-- =============================================================

DROP DATABASE IF EXISTS spending_waste;
CREATE DATABASE spending_waste
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;
USE spending_waste;

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;


-- -------------------------------------------------------------
-- 1. t_user  用户
-- -------------------------------------------------------------
CREATE TABLE t_user (
  user_id        INT UNSIGNED AUTO_INCREMENT COMMENT '用户主键',
  username       VARCHAR(50)      NOT NULL           COMMENT '登录名',
  password_hash  VARCHAR(255)     NOT NULL           COMMENT '口令散列值（禁止存明文）',
  nickname       VARCHAR(50)      DEFAULT NULL       COMMENT '昵称',
  monthly_budget DECIMAL(10,2)    DEFAULT NULL       COMMENT '月度预算，用于超支预警',
  created_at     DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP
                                  ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  UNIQUE KEY uk_user_name (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';


-- -------------------------------------------------------------
-- 2. t_category  消费分类（树形，支持二级）
--    ★ is_durable 是把"统计口径"从代码里搬进数据的关键字段 ★
--      立项时"实物类"是靠 SQL 里手写一串 code 排除（NOT IN ('FOOD','TRAFFIC',...)）
--      来实现的，口径散落在应用层多处，改一次要全文搜索，且极易漏改
--      （实测漏改过一次，样本量从 1255 虚高到 1457）。
--      现在改成分类表上的一个字段：有没有"买回来放着不用"的可能。
--        1 = 耐用品，可能有闲置，纳入标注与建模范围
--        0 = 即时消耗 / 服务，消费发生的瞬间就被消耗掉，闲置率恒为 0
--      → 所有统计统一写 c.is_durable = 1，口径只有这一处
-- -------------------------------------------------------------
CREATE TABLE t_category (
  category_id    INT UNSIGNED AUTO_INCREMENT COMMENT '分类主键',
  parent_id      INT UNSIGNED  DEFAULT NULL      COMMENT '上级分类，NULL 表示一级分类',
  category_code  VARCHAR(20)   NOT NULL          COMMENT '业务编码，如 FOOD / DIGITAL',
  category_name  VARCHAR(50)   NOT NULL          COMMENT '分类名称',
  is_consumption TINYINT(1)    NOT NULL DEFAULT 1 COMMENT '1=消费类 0=非消费类（转账/理财/收入）',
  is_durable     TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '1=耐用品（可能有闲置，纳入标注建模）0=即时消耗/服务（闲置率恒为0）',
  sort_no        SMALLINT      NOT NULL DEFAULT 0 COMMENT '排序号',
  PRIMARY KEY (category_id),
  UNIQUE KEY uk_category_code (category_code),
  UNIQUE KEY uk_category_name (parent_id, category_name),
  KEY idx_category_parent (parent_id),
  KEY idx_category_durable (is_consumption, is_durable),
  CONSTRAINT fk_category_parent FOREIGN KEY (parent_id)
    REFERENCES t_category (category_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='消费分类表（树形）';


-- -------------------------------------------------------------
-- 3. t_category_rule  自动分类关键词规则
--    priority 越小越优先：精确商户词(10) > 品类词(50) > 平台词(90)
--    这样"瑞幸"必然命中餐饮，不会被"淘宝"这类全品类平台词抢走
-- -------------------------------------------------------------
CREATE TABLE t_category_rule (
  rule_id     INT UNSIGNED AUTO_INCREMENT COMMENT '规则主键',
  category_id INT UNSIGNED NOT NULL           COMMENT '命中后归入的分类',
  keyword     VARCHAR(50)  NOT NULL           COMMENT '命中词',
  match_field ENUM('counterparty','product','both')
                           NOT NULL DEFAULT 'both' COMMENT '在哪个字段上匹配',
  priority    SMALLINT     NOT NULL DEFAULT 100      COMMENT '优先级，数值小者先匹配',
  is_enabled  TINYINT(1)   NOT NULL DEFAULT 1        COMMENT '是否启用',
  PRIMARY KEY (rule_id),
  KEY idx_rule_match (is_enabled, priority, keyword),
  CONSTRAINT fk_rule_category FOREIGN KEY (category_id)
    REFERENCES t_category (category_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动分类关键词规则表';


-- -------------------------------------------------------------
-- 4. t_import_batch  账单导入批次
--    file_md5 唯一约束 = 同一份文件不可能被导入两次
-- -------------------------------------------------------------
CREATE TABLE t_import_batch (
  batch_id      INT UNSIGNED AUTO_INCREMENT COMMENT '批次主键',
  user_id       INT UNSIGNED NOT NULL      COMMENT '所属用户',
  source        ENUM('wechat','alipay','bank','manual')
                             NOT NULL        COMMENT '账单来源',
  file_name     VARCHAR(255) NOT NULL      COMMENT '原始文件名',
  file_md5      CHAR(32)     NOT NULL      COMMENT '文件 MD5 指纹，防重复导入',
  encoding      VARCHAR(20)  NOT NULL DEFAULT 'utf-8' COMMENT '源文件编码（支付宝常见 GBK）',
  total_rows    INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '文件解析出的行数',
  inserted_rows INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '实际入库行数',
  skipped_rows  INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '跳过行数（重复/无效/非消费）',
  import_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '导入时间',
  PRIMARY KEY (batch_id),
  UNIQUE KEY uk_batch_file (user_id, file_md5),
  KEY idx_batch_user (user_id),
  CONSTRAINT fk_batch_user FOREIGN KEY (user_id)
    REFERENCES t_user (user_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账单导入批次表';


-- -------------------------------------------------------------
-- 5. t_transaction  交易明细（事实表，系统数据量主体）
--    去重键用应用层生成的 dedup_key，而不是平台订单号
--    原因：微信/支付宝的订单号格式不一，且有缺失（NULL 在 UNIQUE 中不去重）
-- -------------------------------------------------------------
CREATE TABLE t_transaction (
  trans_id     BIGINT UNSIGNED AUTO_INCREMENT COMMENT '交易主键',
  user_id      INT UNSIGNED NOT NULL      COMMENT '所属用户',
  batch_id     INT UNSIGNED NOT NULL      COMMENT '来源导入批次',
  trade_no     VARCHAR(64)  DEFAULT NULL  COMMENT '平台订单号（可能为空）',
  dedup_key    CHAR(32)     NOT NULL      COMMENT '行指纹 = MD5(来源+时间+金额+对方+商品)',
  trade_time   DATETIME     NOT NULL      COMMENT '交易时间',
  counterparty VARCHAR(120) DEFAULT NULL  COMMENT '交易对方',
  product_desc VARCHAR(255) DEFAULT NULL  COMMENT '商品说明',
  direction    ENUM('out','in','neutral') NOT NULL COMMENT '资金方向：支出/收入/不计收支',
  amount       DECIMAL(10,2) NOT NULL     COMMENT '金额（恒为正，方向看 direction）',
  pay_method   VARCHAR(30)  DEFAULT NULL  COMMENT '支付方式：零钱/余额宝/银行卡等',
  trade_status VARCHAR(20)  DEFAULT NULL  COMMENT '交易状态：支付成功/已全额退款/交易关闭',
  category_id  INT UNSIGNED DEFAULT NULL  COMMENT '归类结果',
  category_src ENUM('rule','manual','uncategorized')
                            NOT NULL DEFAULT 'uncategorized' COMMENT '归类来源',
  is_refund    TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=该行本身是退款记录，统计时剔除',
  is_primary   TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '1=计入消费统计 0=资金镜像行（防跨源双计）',
  remark       VARCHAR(255) DEFAULT NULL  COMMENT '备注',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (trans_id),
  UNIQUE KEY uk_trans_dedup (user_id, dedup_key),
  KEY idx_trans_time      (user_id, trade_time),
  KEY idx_trans_category  (user_id, category_id, trade_time),
  KEY idx_trans_direction (user_id, direction, trade_time),
  KEY idx_trans_primary   (user_id, is_primary, direction, trade_time),
  CONSTRAINT fk_trans_user     FOREIGN KEY (user_id)
    REFERENCES t_user (user_id)     ON DELETE CASCADE,
  CONSTRAINT fk_trans_batch    FOREIGN KEY (batch_id)
    REFERENCES t_import_batch (batch_id) ON DELETE RESTRICT,
  CONSTRAINT fk_trans_category FOREIGN KEY (category_id)
    REFERENCES t_category (category_id)  ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易明细表（事实表）';


-- -------------------------------------------------------------
-- 6. t_trans_feature  购买情境特征（一交易一特征行）
--    特征与原始事实分离：改特征不用改原始数据，可整表重算
-- -------------------------------------------------------------
CREATE TABLE t_trans_feature (
  trans_id            BIGINT UNSIGNED NOT NULL COMMENT '对应交易',
  trade_hour          TINYINT UNSIGNED NOT NULL COMMENT '下单小时 0-23',
  trade_weekday       TINYINT UNSIGNED NOT NULL COMMENT '星期 1=周一 .. 7=周日',
  is_night            TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否夜间下单 23:00-05:00',
  is_promo            TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否处于大促窗口（618/双11/双12）',
  amount_log          DECIMAL(8,4)    DEFAULT NULL COMMENT '金额对数变换 ln(1+amount)',
  amt_vs_month_avg    DECIMAL(8,4)    DEFAULT NULL COMMENT '本笔金额 / 当月消费均值',
  days_since_same_cat SMALLINT UNSIGNED DEFAULT NULL COMMENT '距上次同类消费间隔天数',
  same_cat_cnt_30d    SMALLINT UNSIGNED DEFAULT NULL COMMENT '近 30 天同类消费笔数',
  feature_version     VARCHAR(10)     NOT NULL DEFAULT 'v1' COMMENT '特征版本，便于迭代追溯',
  PRIMARY KEY (trans_id),
  CONSTRAINT fk_feat_trans FOREIGN KEY (trans_id)
    REFERENCES t_transaction (trans_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='购买情境特征表';


-- -------------------------------------------------------------
-- 7. t_idle_label  闲置标注（人工标注，模型训练标签）
--    ★ 这是整个项目最核心的数据资产：任何公开数据集都没有这列
--
--    ★ sample_stratum：分层抽样层，配合 v_idle_candidate 使用 ★
--      低于 20 元的小额消费占总笔数 8 成、占总金额 2 成，
--      逐笔标注不现实（约 900 笔，且 ¥2.7 一单根本回忆不起买的是什么），
--      标 >=20 元全部 + 小额分层抽样，再用抽样比加权还原总量。
--      此字段记录"这笔属于哪一层"，是加权与事后审计的唯一依据。
--      'all'       = 全样本层（>=20 元，全部标注）
--      's1_0_5'    = 抽样层 0~5 元
--      's2_5_10'   = 抽样层 5~10 元
--      's3_10_20'  = 抽样层 10~20 元
--      NULL        = 非抽样设计产生（如手工补标）
-- -------------------------------------------------------------
CREATE TABLE t_idle_label (
  label_id    INT UNSIGNED AUTO_INCREMENT COMMENT '标注主键',
  trans_id    BIGINT UNSIGNED NOT NULL  COMMENT '被标注的交易',
  is_idle     TINYINT(1)   NOT NULL     COMMENT '1=闲置 0=在用',
  idle_degree TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '闲置程度 0无 1轻度 2重度 3完全浪费',
  idle_reason ENUM('冲动消费','重复购买','与描述不符','尺寸不合适',
                   '三分钟热度','促销囤货','其他')
                           DEFAULT NULL COMMENT '闲置原因',
  used_days   SMALLINT UNSIGNED DEFAULT NULL COMMENT '实际使用天数（自估）',
  sample_stratum VARCHAR(16) DEFAULT NULL COMMENT '抽样层：all/s1_0_5/s2_5_10/s3_10_20，见 v_idle_candidate',
  label_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '标注时间',
  note        VARCHAR(255) DEFAULT NULL COMMENT '备注',
  PRIMARY KEY (label_id),
  UNIQUE KEY uk_label_trans (trans_id),
  KEY idx_label_idle (is_idle, idle_degree),
  KEY idx_label_stratum (sample_stratum),
  CONSTRAINT fk_label_trans FOREIGN KEY (trans_id)
    REFERENCES t_transaction (trans_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='闲置标注表（模型标签）';


-- -------------------------------------------------------------
-- 8. t_risk_record  购买前预警记录（含事后回填，形成评估闭环）
-- -------------------------------------------------------------
CREATE TABLE t_risk_record (
  risk_id         INT UNSIGNED AUTO_INCREMENT COMMENT '预警主键',
  user_id         INT UNSIGNED NOT NULL  COMMENT '用户',
  product_name    VARCHAR(120) NOT NULL  COMMENT '用户输入想买的东西',
  category_id     INT UNSIGNED DEFAULT NULL COMMENT '所属分类',
  amount          DECIMAL(10,2) NOT NULL COMMENT '拟购买金额',
  plan_time       DATETIME     NOT NULL  COMMENT '计划购买时间',
  risk_score      DECIMAL(5,4) NOT NULL  COMMENT '预测闲置概率 0.0000-1.0000',
  risk_level      ENUM('low','medium','high') NOT NULL COMMENT '风险等级',
  hit_factors     VARCHAR(255) DEFAULT NULL COMMENT '命中的高风险特征说明',
  advice          VARCHAR(255) DEFAULT NULL COMMENT '冷静期建议',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  actual_trans_id BIGINT UNSIGNED DEFAULT NULL COMMENT '事后真实下单的交易 ID',
  actual_is_idle  TINYINT(1)   DEFAULT NULL COMMENT '事后是否确实闲置，用于评估模型准确率',
  PRIMARY KEY (risk_id),
  KEY idx_risk_user   (user_id, created_at),
  KEY idx_risk_level  (risk_level, created_at),
  CONSTRAINT fk_risk_user     FOREIGN KEY (user_id)
    REFERENCES t_user (user_id)         ON DELETE CASCADE,
  CONSTRAINT fk_risk_category FOREIGN KEY (category_id)
    REFERENCES t_category (category_id) ON DELETE SET NULL,
  CONSTRAINT fk_risk_trans    FOREIGN KEY (actual_trans_id)
    REFERENCES t_transaction (trans_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='购买预警记录表（含事后回填）';


SET FOREIGN_KEY_CHECKS = 1;


-- =============================================================
--  视图层：把统计口径固定下来，应用层不再重复写 WHERE
--  统一口径：仅支出 / 剔除退款行 / 剔除资金镜像行(is_primary=0) / 仅支付成功
-- =============================================================

-- 视图 1：月度汇总（同时给出两个口径，避免"消费"与"资金流出"混为一谈）
--   消费口径 consumption_out  —— 只含 is_consumption=1 的分类，是分析用的口径
--   非消费资金流出 non_consumption_out —— 学费/转账/理财，量级很大（实测占月度流出 43%），
--                                        不单列会让人误以为"某个月消费暴涨"
--   未分类行（category_id IS NULL）按消费计入，宁可多算不可漏算
CREATE OR REPLACE VIEW v_monthly_summary AS
SELECT
  t.user_id,
  DATE_FORMAT(t.trade_time, '%Y-%m')      AS ym,
  COUNT(*)                                AS trans_cnt,
  ROUND(SUM(t.amount), 2)                 AS total_out,
  ROUND(SUM(CASE WHEN COALESCE(c.is_consumption, 1) = 1 THEN t.amount ELSE 0 END), 2)
                                          AS consumption_out,
  ROUND(SUM(CASE WHEN COALESCE(c.is_consumption, 1) = 0 THEN t.amount ELSE 0 END), 2)
                                          AS non_consumption_out,
  ROUND(AVG(t.amount), 2)                 AS avg_amount,
  ROUND(MAX(t.amount), 2)                 AS max_amount,
  COUNT(DISTINCT DATE(t.trade_time))      AS active_days
FROM t_transaction t
LEFT JOIN t_category c ON c.category_id = t.category_id
WHERE t.direction = 'out'
  AND t.is_refund = 0
  AND t.is_primary = 1
  AND (t.trade_status IS NULL OR t.trade_status NOT IN ('已全额退款', '交易关闭'))
GROUP BY t.user_id, DATE_FORMAT(t.trade_time, '%Y-%m');


-- 视图 2：各分类闲置率（项目的核心分析口径）
-- idle_rate = 闲置笔数 / 已标注笔数，只统计有标注的记录，避免分母污染
-- ⚠ 这里用 is_durable = 1 而不是 is_consumption = 1：
--   即时消耗类（餐饮/交通/服务）不会有闲置，留在分母里会稀释闲置率
CREATE OR REPLACE VIEW v_category_idle_rate AS
SELECT
  c.category_id,
  c.category_name,
  COUNT(*)                                                        AS labeled_cnt,
  ROUND(SUM(t.amount), 2)                                         AS labeled_amount,
  SUM(CASE WHEN l.is_idle = 1 THEN 1 ELSE 0 END)                   AS idle_cnt,
  ROUND(SUM(CASE WHEN l.is_idle = 1 THEN 1 ELSE 0 END) / COUNT(*), 4) AS idle_rate,
  ROUND(SUM(CASE WHEN l.is_idle = 1 THEN t.amount ELSE 0 END), 2)  AS wasted_amount,
  ROUND(AVG(l.idle_degree), 2)                                    AS avg_idle_degree
FROM t_transaction t
JOIN t_idle_label l ON l.trans_id = t.trans_id
JOIN t_category   c ON c.category_id = t.category_id
WHERE t.direction = 'out'
  AND t.is_refund = 0
  AND t.is_primary = 1
  AND c.is_consumption = 1
  AND c.is_durable = 1
GROUP BY c.category_id, c.category_name;


-- 视图 3：浪费金额排行（找"最该管"的消费）
CREATE OR REPLACE VIEW v_top_waste AS
SELECT
  t.user_id,
  t.trans_id,
  t.trade_time,
  COALESCE(c.category_name, '未分类') AS category_name,
  t.counterparty,
  t.product_desc,
  t.amount,
  l.idle_degree,
  l.idle_reason,
  l.used_days
FROM t_transaction t
JOIN t_idle_label l ON l.trans_id = t.trans_id
LEFT JOIN t_category c ON c.category_id = t.category_id
WHERE l.is_idle = 1
  AND t.is_refund = 0
  AND t.is_primary = 1;


-- 视图 4：模型训练集（特征 + 标签一次取出，Python 端直接读）
CREATE OR REPLACE VIEW v_training_set AS
SELECT
  f.trans_id,
  t.user_id,
  t.category_id,
  t.amount,
  t.trade_time,
  f.trade_hour,
  f.trade_weekday,
  f.is_night,
  f.is_promo,
  f.amount_log,
  f.amt_vs_month_avg,
  f.days_since_same_cat,
  f.same_cat_cnt_30d,
  l.is_idle,
  l.idle_degree
FROM t_trans_feature f
JOIN t_transaction t ON t.trans_id = f.trans_id
JOIN t_idle_label  l ON l.trans_id = f.trans_id
WHERE t.direction = 'out'
  AND t.is_refund = 0
  AND t.is_primary = 1;


-- 视图 5：数据完整性 —— 各来源的月度覆盖情况
--   用途：账单导出很容易缺月份（微信单次可导时间范围有限，实测就漏了半年）。
--   没有这个视图，"7 月消费只有 150 元"这种结论会被当成真实下降，
--   实际上是那几个月根本没有数据。分析前必须先看这张表。
CREATE OR REPLACE VIEW v_source_coverage AS
SELECT
  DATE_FORMAT(t.trade_time, '%Y-%m') AS ym,
  SUM(b.source = 'wechat')           AS wechat_cnt,
  SUM(b.source = 'alipay')           AS alipay_cnt,
  SUM(b.source = 'bank')             AS bank_cnt,
  COUNT(*)                           AS total_cnt
FROM t_transaction t
JOIN t_import_batch b ON b.batch_id = t.batch_id
GROUP BY DATE_FORMAT(t.trade_time, '%Y-%m');


-- =============================================================
--  视图 6~8：标注口径与分层抽样（2026-10-12 新增）
--
--  背景：原先"哪些交易该标注"是靠分析时手写一串 code 排除的，
--        口径散落、改一次要全文搜索，且实测漏改过（样本量 1255 → 虚高 1457）。
--        现在收敛成 v_idle_candidate 一个入口，任何统计都从它出发。
--
--  再一个背景：>=20 元只有 201 笔，占实物金额 74%；
--        小额 900 多笔占 26% 金额，不能直接丢掉（丢了闲置率就是有偏的），
--        也不能逐笔标（¥2.7 一单回忆不起来）。→ 分层抽样 + 事后加权。
-- =============================================================

-- 视图 6：标注候选全集（口径唯一来源）
--   口径：支出 / 非退款 / 计入统计(is_primary=1) / 消费类 / 耐用品 / 支付成功
--   stratum：全样本层 all（>=20 元）与三个抽样层 s1~s3
CREATE OR REPLACE VIEW v_idle_candidate AS
SELECT
  t.trans_id,
  t.user_id,
  t.trade_time,
  t.counterparty,
  t.product_desc,
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
WHERE t.direction = 'out'
  AND t.is_refund = 0
  AND t.is_primary = 1
  AND c.is_consumption = 1
  AND c.is_durable = 1
  AND (t.trade_status IS NULL OR t.trade_status NOT IN ('已全额退款', '交易关闭'));


-- 视图 7：各抽样层的总体权重
--   weight = 层内候选笔数 / 层内已标注笔数，即"一笔标注代表几笔真实交易"
--   ⚠ 层内标注数必须 > 0，否则 NULLIF 会让 weight 变 NULL（该层不可估计）
CREATE OR REPLACE VIEW v_stratum_weight AS
SELECT
  v.stratum,
  COUNT(*)                                                          AS universe_cnt,
  SUM(CASE WHEN l.label_id IS NOT NULL THEN 1 ELSE 0 END)            AS labeled_cnt,
  ROUND(COUNT(*) / NULLIF(SUM(CASE WHEN l.label_id IS NOT NULL THEN 1 ELSE 0 END), 0), 4)
                                                                    AS weight
FROM v_idle_candidate v
LEFT JOIN t_idle_label l ON l.trans_id = v.trans_id
GROUP BY v.stratum;


-- 视图 8：按抽样比加权还原后的分类闲置率（论文里的正式口径）
--   为什么必须加权：小额层抽样了 60/319，直接算笔数比例会让小额层权重被压到 19%，
--   而它在总体中实际占 30%。加权后各层按真实占比回归。
--   est_amount_idle_rate = 闲置金额 / 待考察金额（金额口径，回避小额"笔数多但钱少"的失真）
CREATE OR REPLACE VIEW v_category_idle_rate_weighted AS
SELECT
  c.category_id,
  c.category_name,
  COUNT(*)                                                                    AS labeled_cnt,
  ROUND(SUM(w.weight), 2)                                                     AS est_universe_cnt,
  ROUND(SUM(CASE WHEN l.is_idle = 1 THEN w.weight ELSE 0 END), 2)              AS est_idle_cnt,
  ROUND(SUM(t.amount * w.weight), 2)                                          AS est_total_amount,
  ROUND(SUM(CASE WHEN l.is_idle = 1 THEN t.amount * w.weight ELSE 0 END), 2)   AS est_wasted_amount,
  ROUND(SUM(CASE WHEN l.is_idle = 1 THEN t.amount * w.weight ELSE 0 END)
        / NULLIF(SUM(t.amount * w.weight), 0), 4)                             AS est_amount_idle_rate
FROM t_idle_label l
JOIN t_transaction t   ON t.trans_id   = l.trans_id
JOIN t_category    c   ON c.category_id = t.category_id
JOIN v_idle_candidate v ON v.trans_id  = l.trans_id
JOIN v_stratum_weight w ON w.stratum   = v.stratum
GROUP BY c.category_id, c.category_name;
