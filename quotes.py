# -*- coding: utf-8 -*-
"""行情抓取与解析。

数据源：
- A 股 / 指数：新浪财经 hq.sinajs.cn（GBK/GB18030 文本）
- 伦敦金现：hf_XAU
- 伦铜：hf_CAD
- 备用：东方财富 push2 接口（仅 A 股）

代码格式：
- sh600519 / sz000001 / bj430047 / sh000001（指数）
- hf_XAU（伦敦金现）、hf_CAD（伦铜）、hf_HG（美铜）
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime

SINA_URL = "https://hq.sinajs.cn/list={}"
SINA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "*/*",
}

EM_URL = (
    "https://push2.eastmoney.com/api/qt/stock/get"
    "?secid={secid}&fields=f43,f44,f45,f46,f57,f58,f60,f86&_={ts}"
)
EM_HEADERS = {
    "User-Agent": SINA_HEADERS["User-Agent"],
    "Referer": "https://quote.eastmoney.com/",
}

INDEX_CODES = {
    "sh000001", "sh000300", "sh000905", "sh000016", "sh000688", "sh000852",
    "sz399001", "sz399005", "sz399006", "sz399300", "sz399905",
    "bj899050",
}

# 外盘品种显示名（新浪返回的名字太长，这里统一成常用叫法）
NAME_OVERRIDE = {
    "hf_XAU": "伦敦金现",
    "hf_CAD": "伦铜",
    "hf_HG": "美铜",
    "hf_XAG": "伦敦银",
    "hf_AHD": "伦铝",
    "hf_ZSD": "伦锌",
    "hf_NID": "伦镍",
    "hf_PBD": "伦铅",
    "hf_SND": "伦锡",
}

ALIASES = {
    # 指数
    "上证指数": "sh000001",
    "上证": "sh000001",
    "上证综指": "sh000001",
    "深证成指": "sz399001",
    "深成指": "sz399001",
    "创业板指": "sz399006",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "科创50": "sh000688",
    # 外盘
    "伦敦金": "hf_XAU",
    "伦敦金现": "hf_XAU",
    "现货黄金": "hf_XAU",
    "xau": "hf_XAU",
    "gold": "hf_XAU",
    "伦铜": "hf_CAD",
    "lme铜": "hf_CAD",
    "lme铜价": "hf_CAD",
    "copper": "hf_CAD",
    "美铜": "hf_HG",
    "伦敦银": "hf_XAG",
    "伦铝": "hf_AHD",
}

_LINE_RE = re.compile(r'var\s+hq_str_([A-Za-z0-9_]+)="([^"]*)";')


@dataclass
class Quote:
    code: str
    name: str
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    date: str = ""
    time: str = ""
    kind: str = "stock"          # stock / index / futures
    ok: bool = True              # 是否有有效报价（停牌时为 False）

    @property
    def change(self) -> float:
        if not self.prev_close:
            return 0.0
        return self.price - self.prev_close

    @property
    def pct(self) -> float:
        if not self.prev_close:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100.0

    @property
    def currency(self) -> str:
        return "USD" if self.code.startswith("hf_") else "CNY"

    @property
    def unit(self) -> str:
        if self.code == "hf_XAU":
            return "美元/盎司"
        if self.code in ("hf_CAD", "hf_AHD", "hf_ZSD", "hf_NID", "hf_PBD"):
            return "美元/吨"
        if self.code == "hf_HG":
            return "美分/磅"
        return "元"


def normalize_symbol(raw: str) -> str:
    """把用户输入统一成新浪代码。"""
    s = (raw or "").strip().lower().replace(" ", "")
    if not s:
        raise ValueError("代码不能为空")
    if s in ALIASES:
        return ALIASES[s]
    if s.startswith("hf_"):
        return "hf_" + s[3:].upper()
    if "." in s:  # 600519.sh / 000001.sz
        a, b = s.split(".", 1)
        if a.isdigit() and b in ("sh", "sz", "bj"):
            s = b + a
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj") and s[2:].isdigit():
        return s
    if s.isdigit() and len(s) == 6:
        first = s[0]
        if first in "69":
            return "sh" + s
        if first in "03":
            return "sz" + s
        if first in "48":
            return "bj" + s
        if first == "5":
            return "sh" + s
        if first == "1":
            return "sz" + s
    raise ValueError(f"无法识别的代码：{raw}")


def display_name(code: str, names: dict | None = None) -> str:
    if code in NAME_OVERRIDE:
        return NAME_OVERRIDE[code]
    if names and names.get(code):
        return names[code]
    if code.startswith("hf_"):
        return code
    return code[2:] if len(code) > 2 else code


def _num(value) -> float:
    try:
        if value is None or value == "" or value == "-":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_a_share(code: str, fields: list[str]) -> Quote | None:
    if len(fields) < 6:
        return None
    name = fields[0].strip()
    open_ = _num(fields[1])
    prev = _num(fields[2])
    price = _num(fields[3])
    high = _num(fields[4])
    low = _num(fields[5])
    date = fields[30].strip() if len(fields) > 30 else ""
    tm = fields[31].strip() if len(fields) > 31 else ""
    if price <= 0 and prev <= 0:
        return None
    kind = "index" if code in INDEX_CODES else "stock"
    return Quote(
        code=code,
        name=name,
        price=price,
        prev_close=prev,
        open=open_,
        high=high,
        low=low,
        date=date,
        time=tm,
        kind=kind,
        ok=price > 0,
    )


def _parse_foreign(code: str, fields: list[str]) -> Quote | None:
    if len(fields) < 7:
        return None
    price = _num(fields[0])
    # 新浪外盘字段：0 最新价，1 买价/昨收，2 开盘，4 最高，5 最低，
    # 6 时间，7 昨收，8 昨结，12 日期，13 名称
    prev = _num(fields[7]) if len(fields) > 7 else 0.0
    if prev <= 0:
        prev = _num(fields[1])
    if prev <= 0 and len(fields) > 8:
        prev = _num(fields[8])
    high = _num(fields[4])
    low = _num(fields[5])
    tm = fields[6].strip() if len(fields) > 6 else ""
    date = fields[12].strip() if len(fields) > 12 else ""
    name = NAME_OVERRIDE.get(code) or (fields[13].strip() if len(fields) > 13 else code)
    if price <= 0:
        return None
    return Quote(
        code=code,
        name=name,
        price=price,
        prev_close=prev,
        open=_num(fields[2]),
        high=high,
        low=low,
        date=date,
        time=tm,
        kind="futures",
        ok=True,
    )


def parse_sina(text: str) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    for match in _LINE_RE.finditer(text):
        code, payload = match.group(1), match.group(2)
        if not payload:
            continue
        fields = payload.split(",")
        try:
            quote = _parse_foreign(code, fields) if code.startswith("hf_") else _parse_a_share(code, fields)
        except Exception:
            quote = None
        if quote:
            out[code] = quote
    return out


def fetch_sina(codes: list[str], timeout: float = 8.0) -> dict[str, Quote]:
    url = SINA_URL.format(",".join(codes))
    req = urllib.request.Request(url, headers=SINA_HEADERS)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    return parse_sina(raw.decode("gb18030", errors="replace"))


def fetch_eastmoney(codes: list[str], timeout: float = 6.0) -> dict[str, Quote]:
    """A 股备用源。只处理 sh/sz，不处理外盘。"""
    out: dict[str, Quote] = {}
    ctx = ssl.create_default_context()
    for code in codes:
        if code.startswith("hf_") or code[:2] not in ("sh", "sz"):
            continue
        secid = ("1." if code.startswith("sh") else "0.") + code[2:]
        url = EM_URL.format(secid=secid, ts=int(time.time() * 1000))
        try:
            req = urllib.request.Request(url, headers=EM_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            continue
        obj = data.get("data") if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            continue
        price = _num(obj.get("f43")) / 100.0
        prev = _num(obj.get("f60")) / 100.0
        if price <= 0:
            continue
        name = str(obj.get("f58") or code[2:])
        out[code] = Quote(
            code=code,
            name=name,
            price=price,
            prev_close=prev,
            open=_num(obj.get("f46")) / 100.0,
            high=_num(obj.get("f44")) / 100.0,
            low=_num(obj.get("f45")) / 100.0,
            date=datetime.now().strftime("%Y-%m-%d"),
            time=str(obj.get("f86") or ""),
            kind="index" if code in INDEX_CODES else "stock",
            ok=True,
        )
    return out


def fetch_quotes(codes: list[str]) -> dict[str, Quote]:
    """抓取全部代码，失败时自动重试并回退到备用源。"""
    codes = list(dict.fromkeys(codes))
    if not codes:
        return {}
    result: dict[str, Quote] = {}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            result = fetch_sina(codes)
            if result:
                break
        except Exception as exc:  # 网络抖动时重试一次
            last_error = exc
            time.sleep(0.35 if attempt == 0 else 0)
    missing = [c for c in codes if c not in result]
    if missing:
        try:
            result.update(fetch_eastmoney(missing))
        except Exception as exc:
            last_error = last_error or exc
    if not result:
        raise last_error or RuntimeError("行情接口没有返回数据")
    return result
