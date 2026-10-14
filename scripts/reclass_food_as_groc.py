#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把标注候选池里的「吃的东西」改判为 GROC（食品生鲜），自动移出闲置标注范围。

为什么需要
----------
账单源（微信/支付宝）在拼多多这类电商上只记「拼多多平台商户 / 先用后付」，
商品名靠 scripts/ 那套拼多多订单解析补全（t_goods_detail）。补全之后，
标注工作台里冒出大量**零食、生鲜、饮料**——即时消耗品不存在「买回来闲置」，
让用户逐笔标注纯属浪费。

设计
----
- 命中 → 写 `sql/06_food_as_groc.sql`，把 t_transaction.category_id 改成 GROC。
  GROC 的 is_durable=0，视图 v_idle_candidate 带 `c.is_durable=1`，
  这些行自动掉出候选池，不需要在工作台里再解释。
- 只改 category_id + category_src='rule:food'，**不动** product_desc（它是
  dedup_key 的输入，改了重导账单会翻倍）。
- 幂等：WHERE 带 category_id <> GROC，重复执行结果一致。

判定规则（保守优先，宁漏勿错）
------------------------------
v_idle_candidate 里有两个商品名字段，**可信度完全不同**：

  goods_title  唯一命中的真实商品名          → 可直接采信
  alt_titles   同金额的多个候选，用 " / " 拼  → 不能只看任一条

踩过的坑：`适用OPPO…入耳式有线耳机 / 憨豆熊小鱼干零食 / 泰国龙眼`
真身是耳机，若按「任一候选命中食品」就会把耳机当零食移出。

所以：
  1. goods_title 命中食品且不被排除词挡下            → 改判（confidence=unique）
  2. goods_title 为空时，拆开 alt_titles 的每一段，
     **所有**候选都命中食品（且过滤掉『送货上门』类噪声）→ 改判（=all_cand）
  3. 其余保持待标，只在报告里列出「候选含食品」供人工看

排除词
------
中文短词极易误伤（"笔"命中"蜡笔小新"，"锅"命中"空气炸锅食材"，"苹果"命中
"苹果手机"，"牛奶"命中"牛奶绒"面料）。因此排除词一律用**具体词**，
不用「笔/锅/碗/杯/瓶/苹果」这类会大量误伤的裸词。

用法
----
    python scripts/reclass_food_as_groc.py                 # 只扫描，打印报告
    python scripts/reclass_food_as_groc.py --write-sql     # 顺便生成 06 的 SQL
"""
import argparse
import collections
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)                      # 让 `app.*` 可导入

from app.services.importers import pipeline  # noqa: E402

# -------------------------------------------------------------
# 一、食品关键词（两字及以上具体词；裸"苹果"已剔除，见排除词注释）
# -------------------------------------------------------------
FOOD_KW = (
    # 水果
    "水果", "香蕉", "西瓜", "葡萄", "草莓", "芒果", "火龙果", "荔枝", "柚子",
    "香梨", "猕猴桃", "圣女果", "西红柿", "柑橘", "橙子", "哈密瓜", "甜瓜", "香瓜",
    "蜜瓜", "椰子", "柠檬", "蓝莓", "樱桃", "龙眼", "桂圆", "车厘子", "杨梅", "枇杷",
    "菠萝", "无花果", "百香果", "石榴", "甘蔗", "山楂", "提子", "蛇果", "鲜果", "果切",
    "脐橙", "沃柑", "砂糖橘", "砂糖桔", "皇帝柑", "丑橘", "红富士", "阿克苏",
    # 蔬菜
    "蔬菜", "生菜", "菠菜", "芹菜", "韭菜", "辣椒", "洋葱", "蘑菇", "黄瓜", "土豆",
    "白菜", "玉米", "红薯", "南瓜", "山药", "胡萝卜", "番茄", "茄子", "豆角", "香菇",
    "娃娃菜", "西蓝花", "西兰花", "芦笋", "莴笋", "莲藕", "青菜", "紫薯", "芋头",
    # 肉蛋水产
    "牛肉", "猪肉", "羊肉", "鸡肉", "鸡胸", "鸡腿", "鸡翅", "鸡排", "鸡柳", "鸡块",
    "鸭脖", "鸭腿", "鸭掌", "鸡爪", "凤爪", "猪蹄", "排骨", "肉肠", "烤肠", "热狗",
    "培根", "火腿", "香肠", "肉干", "肉脯", "肉松", "鸡蛋", "鹌鹑蛋", "卤蛋", "咸蛋",
    "皮蛋", "变蛋", "鱼丸", "虾滑", "虾仁", "龙虾", "螃蟹", "鱿鱼", "带鱼", "鲈鱼",
    "黄鱼", "羊卷", "肥牛",
    # 乳品饮料
    "牛奶", "酸奶", "纯奶", "钙奶", "奶优", "营养快线", "乳酸菌", "早餐奶", "优酸乳",
    "奶粉", "炼乳", "奶油", "奶酪", "芝士", "饮料", "汽水", "可乐", "雪碧", "芬达",
    "果汁", "果茶", "奶茶", "咖啡", "矿泉水", "纯净水", "饮用水", "气泡水", "元气森林",
    "农夫山泉", "怡宝", "豆奶", "椰汁", "椰浆", "核桃露", "六个核桃", "娃哈哈", "蒙牛",
    "伊利", "君乐宝", "科迪", "认养一头牛", "维他", "美汁源", "果粒橙", "脉动",
    # 零食
    "零食", "辣条", "薯片", "薯条", "膨化", "坚果", "瓜子", "花生", "开心果", "腰果",
    "巴旦木", "夏威夷果", "巧克力", "糖果", "软糖", "棒棒糖", "口香糖", "果冻", "布丁",
    "蜜饯", "话梅", "果脯", "海苔", "辣片", "豆干", "素肉", "魔芋爽", "小鱼仔", "鱼干",
    "鸭舌", "牛肉粒", "锅巴", "米饼", "虾条", "雪饼", "麦丽素", "小馒头",
    # 烘焙糕点
    "面包", "吐司", "蛋糕", "饼干", "曲奇", "蛋挞", "桃酥", "糕点", "点心", "月饼",
    "鲜花饼", "酥饼", "米糕", "沙琪玛", "麻薯", "麻糍", "欧包", "贝果", "华夫",
    "老婆饼", "凤梨酥", "蛋黄酥", "绿豆糕", "泡芙", "薄饼", "麻花",
    # 方便速食
    "方便面", "泡面", "拌面", "挂面", "拉面", "米线", "米粉", "螺蛳粉", "酸辣粉",
    "刀削面", "热干面", "意大利面", "饺子", "馄饨", "汤圆", "粽子", "烧麦", "馒头",
    "包子", "手抓饼", "速食", "自热", "自嗨锅", "午餐肉", "罐头", "榨菜", "咸菜",
    "火腿肠", "脆骨", "锅贴", "煎饼", "凉皮", "粉条", "粉丝", "粉面", "浇头",
    # 调味粮油
    "大米", "面粉", "食用油", "花生油", "玉米油", "橄榄油", "酱油", "生抽", "老抽",
    "料酒", "蚝油", "味精", "鸡精", "调料", "调味", "火锅底料", "辣椒酱", "番茄酱",
    "沙拉酱", "芝麻酱", "蜂蜜", "干果", "杂粮", "燕麦", "麦片", "藜麦", "五谷",
    "豆浆粉", "烘培", "烘焙", "酵母", "紫菜", "木耳", "银耳", "红枣", "枸杞", "莲子",
    "豆豉", "腐乳", "花椒", "八角", "调味料", "鸡蛋灌饼",
    # 酒水
    "啤酒", "白酒", "红酒", "果酒", "梅子酒", "米酒", "清酒", "葡萄酒", "鸡尾酒",
    "梅见", "青梅酒", "锐澳", "RIO", "黄酒", "伏特加",
    # 茶
    "花茶", "养生茶", "菊花茶", "金银花", "绿茶", "碧螺春", "毛峰", "花草茶",
    "皇菊", "石斛", "茶叶", "红茶", "普洱", "乌龙",
    # 泛化（只用真正指向食物的词）
    "生鲜", "速冻", "冷冻", "冰鲜", "即食", "熟食", "卤味", "预制菜",
    "芋圆", "冰粉", "丸子", "雪糕", "冰淇淋", "奶昔", "芋泥", "调味包",
)

# 命中排除词 → 判为「不是给自己吃的食品」。只收具体词：
# 用 "笔/锅/碗/杯/瓶/苹果" 这类裸词会把 蜡笔小新 / 空气炸锅食材 / 苹果手机 一起误伤。
DENY_KW = (
    # 宠物
    "猫粮", "狗粮", "猫砂", "犬粮", "宠物", "幼猫", "猫咪", "狗狗",
    # 个护清洁
    "洗发", "沐浴", "牙膏", "洗衣", "清洁", "柔顺", "护发", "护肤", "面膜", "精华",
    "香皂", "肥皂", "洗手", "身体乳", "护手霜", "面霜", "乳液", "爽肤水", "防晒",
    "沐浴露", "洁面", "香水", "口红", "唇膏", "指甲油", "美甲",
    # 文具绘画（裸"笔"会误伤蜡笔小新）
    "铅笔", "水彩笔", "画笔", "炭笔", "马克笔", "中性笔", "钢笔", "签字笔", "蜡笔",
    "彩笔", "勾线笔", "针管笔", "绘图笔", "字帖", "教材", "图书", "笔记本", "画材",
    "颜料", "玩具", "文具", "贴纸", "手账", "油画", "素描",
    # 餐具厨具（裸"锅碗杯瓶"会误伤火锅食材/杯装奶茶）
    "陶瓷碗", "饭碗", "餐盘", "炒锅", "不粘锅", "珐琅锅", "锅具", "蒸锅", "菜刀",
    "菜板", "水果刀", "水杯", "保温杯", "马克杯", "杯子", "吸管杯", "置物", "收纳",
    "衣架", "抹布", "牙签", "竹签", "签子", "烧烤签",
    # 服饰家纺（"茶叶枕头/华夫格裤/牛奶绒被"这类关键词误伤高发区）
    "裤", "裙", "外套", "卫衣", "衬衫", "T恤", "羽绒", "袜", "帽",
    "枕头", "抱枕", "靠枕", "床垫", "床单", "床罩", "被套", "被子", "被芯", "褥",
    "凉席", "坐垫", "桌垫", "毛巾", "浴巾", "华夫格",
    # 饰品（"纯银耳扣"会被当"银耳"）
    "耳环", "耳钉", "耳扣", "耳饰", "耳圈", "项链", "手镯", "戒指", "发夹", "发卡",
    "发圈", "头绳", "头箍", "纯银", "925",
    # 医药
    "祛痣", "祛痘", "药膏", "口罩", "体温计", "凝胶", "胶囊", "药",
    # 数码配件（"苹果""小米"作商标时极易误伤）
    "手机壳", "保护壳", "贴膜", "钢化膜", "数据线", "充电", "耳机", "手机",
    "安卓", "蓝牙", "U盘", "内存卡",
    # 颜色/名号
    "糖果色", "奶茶色", "咖啡色", "巧克力色", "奶茶店", "牛奶绒", "奶酪单词",
    "奶油色", "蜜桃臀", "西瓜红", "柠檬黄", "车厘子色",
    # 器械
    "电子秤", "厨房秤", "称量", "烘焙称", "酸奶机", "面包机", "蛋糕模具", "烤箱",
    # 非食品的商品名
    "单词", "会员", "年卡",
)

# 候选段里的噪声（不是商品），拆分后先剔除
ALT_NOISE = ("送货上门", "免运费", "退运费", "退款", "补差", "降价", "运费险",
             "先用后付", "包邮", "优惠券", "凑单")

FIELD_WEIGHT = {"goods_title": 3, "alt_titles": 2, "product_desc": 1, "counterparty": 1}


def hits_of(text, kws):
    return [k for k in kws if k in text]


def is_food(text):
    """单段文本是否为食品 → (bool, 命中词, 排除词)"""
    if not text:
        return False, [], []
    fh = hits_of(text, FOOD_KW)
    if not fh:
        return False, [], []
    dh = hits_of(text, DENY_KW)
    return (not dh), fh, dh


def split_alts(alt: str):
    """拆开多候选串（用 ' / ' 分隔），剔除噪声段。"""
    parts = [p.strip() for p in alt.split(" / ")]
    return [p for p in parts if p and not any(n in p for n in ALT_NOISE)]


def fetch_rows(conn):
    # 注意：无参数 execute() 时 pymysql 不还原 %%，这里必须写单 %
    sql = """
        SELECT v.trans_id, DATE_FORMAT(v.trade_time,'%Y-%m-%d') AS tm,
               COALESCE(v.counterparty,'') AS cp,
               COALESCE(v.product_desc,'') AS pd,
               COALESCE(v.goods_title,'')  AS gt,
               COALESCE(v.alt_titles,'')   AS alt,
               v.amount, v.category_code, v.category_name, v.stratum
        FROM v_idle_candidate v
        ORDER BY v.amount DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["amount"] = float(d["amount"])
            out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser(description="候选池里的食品 → 改判 GROC")
    ap.add_argument("--write-sql", action="store_true", help="生成 sql/06_food_as_groc.sql")
    ap.add_argument("--out", default="sql/06_food_as_groc.sql")
    args = ap.parse_args()

    conn = pipeline.connect()
    rows = fetch_rows(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT category_id FROM t_category WHERE category_code='GROC'")
        cat_groc = cur.fetchone()[0]
    conn.close()

    decided, alt_food_only, review, kept = [], [], [], []
    for r in rows:
        ok, fh, dh = is_food(r["gt"])
        if ok:
            r["_why"] = "unique"
            decided.append(r)
            continue
        if r["gt"]:
            # 商品名明确存在且不是食品 → 一定不改判（避免苹果手机/自动铅笔那类误伤）
            r["_dh"] = dh
            kept.append(r)
            continue
        parts = split_alts(r["alt"])
        if parts and all(is_food(p)[0] for p in parts):
            r["_why"] = "all_cand"
            alt_food_only.append(r)
            continue
        foodish = [p for p in parts if is_food(p)[0]]
        if foodish:
            r["_foodish"] = foodish
            review.append(r)
        else:
            kept.append(r)

    final = decided + alt_food_only
    print(f"候选池 {len(rows)} 笔")
    print(f"  ① 商品名唯一命中食品        {len(decided):4} 笔  ¥{sum(r['amount'] for r in decided):,.2f}  → 改判")
    print(f"  ② 候选全部是食品            {len(alt_food_only):4} 笔  ¥{sum(r['amount'] for r in alt_food_only):,.2f}  → 改判")
    print(f"  ③ 候选里混着食品（需人工看）{len(review):4} 笔  ¥{sum(r['amount'] for r in review):,.2f}  → 保持待标")
    print(f"  ④ 明确不是食品              {len(kept):4} 笔  ¥{sum(r['amount'] for r in kept):,.2f}  → 保持待标")
    print(f"\n  改判合计 {len(final)} 笔 → 候选池 {len(rows)} ⇒ {len(rows) - len(final)} 笔")

    def show(title, arr, n, extra=None):
        print(f"\n=== {title} ===")
        for r in arr[:n]:
            name = (r["gt"] or r["alt"] or r["pd"])[:44]
            tail = f" ← {','.join(r['_why'].split())}" if extra else ""
            print(f"  ¥{r['amount']:>8.2f} [{r['category_code']:<7}] {name:<46}{tail}")

    show("① 商品名唯一命中（前 40）", decided, 40, extra=True)
    show("② 候选全部是食品（全部）", alt_food_only, 200)
    print("\n=== ③ 候选混着食品（保持待标，人工扫一眼）===")
    for r in review[:25]:
        print(f"  ¥{r['amount']:>7.2f} 食品候选={len(r['_foodish'])}/{len(split_alts(r['alt']))} | {r['alt'][:70]}")

    if args.write_sql:
        stmts = []
        for r in final:
            stmts.append(
                f"UPDATE t_transaction SET category_id = {cat_groc}, category_src = 'rule'\n"
                f" WHERE trans_id = {r['trans_id']} AND category_id <> {cat_groc};"
                f"   -- ¥{r['amount']:.2f} [{r['_why']}] {(r['gt'] or r['alt'])[:28]}"
            )
        head = f"""-- =============================================================
--  食品改判  sql/06_food_as_groc.sql
--  由 scripts/reclass_food_as_groc.py --write-sql 生成，请勿手改
--
--  背景：标注候选池里的零食/生鲜/饮料是即时消耗品，不存在「闲置」，
--        不该占用人工标注。（GROC 的 is_durable=0，改判后自动掉出
--        v_idle_candidate，工作台不再出现。）
--  本批 {len(stmts)} 笔，金额 ¥{sum(r['amount'] for r in final):,.2f}
--    其中 商品名唯一命中 {len(decided)} / 候选全部是食品 {len(alt_food_only)}
--  幂等：WHERE 带 category_id <> {cat_groc}
--  注意：只改 category_id，不动 product_desc（它是 dedup_key 的输入）
--  注意：category_src 是 ENUM(rule/manual/uncategorized)，所以填 rule，
--        不写 'rule:food'（会 Data truncated）；来源靠本文件注释区分。
-- =============================================================

USE spending_waste;
SET NAMES utf8mb4;

"""
        out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        with io.open(out, "w", encoding="utf-8") as f:
            f.write(head + "\n".join(stmts) + "\n")
        print(f"\n已生成 {out}（{len(stmts)} 条 UPDATE）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
