"""账单导入器。

三个来源各一个模块，对外暴露同一套接口：

    parse(path) -> List[RawRecord]

用法：
    from app.services.importers import wechat, alipay, icbc
    recs = wechat.parse(r"...\\wechat.xlsx")

新增来源（例如其他银行）时，照抄 icbc.py 的结构加一个模块，
并在 PARSERS 里注册即可，上层 pipeline 不用改。
"""

from . import alipay, base, icbc, wechat

# 来源标识 → 解析器。pipeline 按这个表分发
PARSERS = {
    "wechat": wechat.parse,
    "alipay": alipay.parse,
    "bank": icbc.parse,
}

__all__ = ["wechat", "alipay", "icbc", "base", "PARSERS"]
