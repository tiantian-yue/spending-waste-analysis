-- =============================================================
--  种子数据：分类字典 + 自动分类规则
--  依赖：请先执行 01_schema.sql
--  可重复执行（先清空再插入）
-- =============================================================
USE spending_waste;
SET NAMES utf8mb4;

SET FOREIGN_KEY_CHECKS = 0;
TRUNCATE TABLE t_category_rule;
TRUNCATE TABLE t_category;
SET FOREIGN_KEY_CHECKS = 1;


-- -------------------------------------------------------------
-- 一、消费分类（is_consumption = 1）
--     实体类消费才有"闲置"概念，餐饮交通属于即时消费，单独区分
-- -------------------------------------------------------------
INSERT INTO t_category (category_id, parent_id, category_code, category_name, is_consumption, sort_no) VALUES
  ( 1, NULL, 'FOOD',    '餐饮',   1,  10),
  ( 2, NULL, 'TRAFFIC', '交通',   1,  20),
  ( 3, NULL, 'APPAREL', '服饰美妆', 1,  30),
  ( 4, NULL, 'DIGITAL', '数码电器', 1,  40),
  ( 5, NULL, 'STUDY',   '学习文教', 1,  50),
  ( 6, NULL, 'LIFE',    '生活日用', 1,  60),
  ( 7, NULL, 'MED',     '医疗健康', 1,  70),
  ( 8, NULL, 'ENT',     '娱乐社交', 1,  80),
  ( 9, NULL, 'HOBBY',   '兴趣爱好', 1,  90),
  (10, NULL, 'OTHER',   '其他',   1, 999);

-- 非消费类（is_consumption = 0）：不参与消费统计与闲置分析
INSERT INTO t_category (category_id, parent_id, category_code, category_name, is_consumption, sort_no) VALUES
  (90, NULL, 'TRANSFER', '转账红包', 0, 900),
  (91, NULL, 'FINANCE',  '金融理财', 0, 910),
  (92, NULL, 'INCOME',   '收入',    0, 920);


-- -------------------------------------------------------------
-- 二、自动分类规则
--     优先级设计：
--       10  = 精确品牌/商户词，指向唯一分类，必须最优先
--       50  = 品类特征词
--       90  = 全品类平台词（淘宝/京东什么都卖），只在没有更精确规则时兜底
--     同一关键词命中多条规则时，取 priority 最小者
-- -------------------------------------------------------------
INSERT INTO t_category_rule (category_id, keyword, match_field, priority) VALUES
  -- 精确商户 → 餐饮
  ( 1, '瑞幸',      'both', 10), ( 1, '星巴克',   'both', 10),
  ( 1, '蜜雪冰城',  'both', 10), ( 1, '喜茶',     'both', 10),
  ( 1, '麦当劳',    'both', 10), ( 1, '肯德基',   'both', 10),
  ( 1, '海底捞',    'both', 10), ( 1, '华莱士',   'both', 10),
  -- 品类词 → 餐饮
  ( 1, '餐饮',      'both', 50), ( 1, '食堂',     'both', 50),
  ( 1, '外卖',      'both', 50), ( 1, '食品',     'product', 50),
  ( 1, '美团',      'both', 50), ( 1, '饿了么',   'both', 50),
  ( 1, '咖啡',      'both', 50),

  -- 交通
  ( 2, '滴滴',      'both', 10), ( 2, '高德',     'both', 50),
  ( 2, '地铁',      'both', 50), ( 2, '公交',     'both', 50),
  ( 2, '12306',     'both', 50), ( 2, '铁路',     'both', 50),
  ( 2, '打车',      'both', 50), ( 2, '加油',     'both', 50),
  ( 2, '共享单车',  'both', 50),

  -- 服饰美妆
  ( 3, '优衣库',    'both', 10), ( 3, '屈臣氏',   'both', 10),
  ( 3, '服饰',      'product', 50), ( 3, '服装',  'both', 50),
  ( 3, '鞋',        'product', 50), ( 3, '化妆',  'both', 50),
  ( 3, '护肤',      'both', 50), ( 3, '美妆',     'both', 50),
  ( 3, '唯品会',    'both', 50),

  -- 数码电器
  ( 4, '小米',      'both', 10), ( 4, '苹果',     'both', 10),
  ( 4, '华为',      'both', 10), ( 4, '联想',     'both', 10),
  ( 4, '数码',      'both', 50), ( 4, '电器',     'both', 50),
  ( 4, '耳机',      'product', 50), ( 4, '显示器', 'product', 50),
  ( 4, '键盘',      'product', 50), ( 4, '鼠标',  'product', 50),

  -- 学习文教
  ( 5, '当当',      'both', 50), ( 5, '图书',     'product', 50),
  ( 5, '书店',      'both', 50), ( 5, '打印',     'both', 50),
  ( 5, '知网',      'both', 50), ( 5, '考试',     'both', 50),
  ( 5, '培训',      'both', 50), ( 5, '文教',     'both', 50),
  ( 5, '文具',      'product', 50),

  -- 生活日用
  ( 6, '超市',      'both', 50), ( 6, '便利店',   'both', 50),
  ( 6, '物业',      'both', 50), ( 6, '水电',     'both', 50),
  ( 6, '日用',      'both', 50), ( 6, '洗护',     'product', 50),
  ( 6, '纸巾',      'product', 50),

  -- 医疗健康
  ( 7, '药房',      'both', 50), ( 7, '药店',     'both', 50),
  ( 7, '医院',      'both', 50), ( 7, '挂号',     'both', 50),
  ( 7, '体检',      'both', 50),

  -- 娱乐社交
  ( 8, '电影',      'both', 50), ( 8, '影院',     'both', 50),
  ( 8, '腾讯视频',  'both', 50), ( 8, '网易云',   'both', 50),
  ( 8, 'Steam',     'both', 10), ( 8, '游戏',     'both', 50),
  ( 8, 'KTV',       'both', 50), ( 8, '会员',     'product', 50),

  -- 兴趣爱好（本项目闲置率最高的品类，值得单独拆细）
  ( 9, '滑雪',      'both', 50), ( 9, '雪具',     'both', 50),
  ( 9, '摄影',      'both', 50), ( 9, '镜头',     'product', 50),
  ( 9, '三脚架',    'product', 50), ( 9, '羽毛球', 'both', 50),
  ( 9, '钢琴',      'both', 50), ( 9, '乐器',     'both', 50),
  ( 9, '水彩',      'both', 50), ( 9, '画材',     'both', 50),
  ( 9, '健身',      'both', 50), ( 9, '户外',     'both', 50),
  ( 9, '露营',      'both', 50),

  -- 全品类平台（兜底，优先级最低）
  (10, '淘宝',      'both', 90), (10, '天猫',     'both', 90),
  (10, '京东',      'both', 90), (10, '拼多多',   'both', 90),
  (10, '抖音',      'both', 90), (10, '闲鱼',     'both', 90),

  -- 非消费类（优先级设为 1，必须先于一切消费规则识别出来）
  (90, '转账',      'both',  1), (90, '红包',     'both',  1),
  (90, '亲情卡',    'both',  1),
  (91, '余额宝',    'both',  1), (91, '基金',     'both',  1),
  (91, '还款',      'both',  1), (91, '理财',     'both',  1),
  (92, '退款',      'both',  1), (92, '收款',     'both',  1);


-- -------------------------------------------------------------
-- 三、初始化一个本地开发用户
--     用户名 demo / 口令 123456
--     散列由 werkzeug.security.generate_password_hash 生成，并已通过
--     check_password_hash 反向自校验，可真实登录
--     ⚠ 仅开发环境使用；正式环境必须更换
-- -------------------------------------------------------------
INSERT INTO t_user (username, password_hash, nickname, monthly_budget) VALUES
  ('demo',
   'scrypt:32768:8:1$ntVbUshruc6Iscgo$e3e32515b7b8178751769f092bc0baf65b5abbecb0cfbd53e711613ae31934349001c0ce83d34adc2816b6118c30a1f05f6de90f7c9a83eb9445433340ad4532',
   '本机用户', 2000.00);


-- -------------------------------------------------------------
-- 四、自检：确认结构与种子都到位
-- -------------------------------------------------------------
SELECT '表数量' AS item, COUNT(*) AS cnt
  FROM information_schema.tables
 WHERE table_schema = 'spending_waste' AND table_type = 'BASE TABLE'
UNION ALL
SELECT '视图数量', COUNT(*)
  FROM information_schema.views
 WHERE table_schema = 'spending_waste'
UNION ALL
SELECT '分类数', COUNT(*) FROM t_category
UNION ALL
SELECT '规则数', COUNT(*) FROM t_category_rule
UNION ALL
SELECT '用户数', COUNT(*) FROM t_user;
