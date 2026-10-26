"""文本规范化:清洗 t.co、语言启发式、embedding 输入构造、时间解析。"""

import re
from datetime import datetime, timezone

TCO_RE = re.compile(r"https?://t\.co/\w+")
WS_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """去掉 t.co 短链、行内空白折叠;保留换行(推文原始格式)。"""
    if not text:
        return ""
    lines = []
    for ln in TCO_RE.sub(" ", text).splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / max(len(text), 1)


def detect_lang(text: str) -> str:
    """粗分类:zh / en / mixed / other,只为报告统计,不追求准确。"""
    r = cjk_ratio(text)
    if r > 0.15:
        return "zh"
    if r > 0.02:
        return "mixed"
    letters = sum(ch.isalpha() and ch.isascii() for ch in text)
    if letters > max(len(text) * 0.3, 3):
        return "en"
    return "other"


TWITTER_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"


def to_iso_utc(value) -> str | None:
    """把各种时间表示统一成 'YYYY-MM-DD HH:MM:SS'(UTC,字典序可比较)。"""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (ValueError, OSError):
            return None
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    s = str(value).strip()
    try:  # ISO 8601(含 +00:00 / Z / 毫秒)统一入口
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    for fmt in (TWITTER_TIME_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def build_embed_text(handle: str | None, text: str, quoted: str | None = None) -> str:
    """拼接作者与(可选)引用推文,让短文本向量更有区分度;换行折叠为空格。"""
    parts = []
    if handle:
        parts.append(f"@{handle}")
    if text:
        parts.append(" ".join(text.split()))
    if quoted:
        parts.append("[引用] " + " ".join(quoted.split()))
    return " ".join(parts)


def tweet_url(handle: str | None, tweet_id: str) -> str:
    h = handle or "i"
    return f"https://x.com/{h}/status/{tweet_id}"
