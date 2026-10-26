"""官方存档 ZIP/目录解析(后备导入路径;主路径是 sync 的 GraphQL 增量)。

X 的存档格式随年份变过多次,这里做容错处理:
- data/bookmarks.json(新,纯数组)或 data/bookmarks.js(旧,window.YTD 前缀)
- 条目可能是 {"bookmark": {...}} 包装,也可能是扁平对象
- 条目可能带全文(full_text 等),也可能只有 id + bookmarked_at(此时尝试用
  data/tweets.js 补内容,补不上的计入 no_content)
"""

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import textproc

KNOWN_FILES = {
    "bookmarks": ("data/bookmarks.json", "data/bookmarks.js"),
    "tweets": ("data/tweets.json", "data/tweets.js"),
}


def _read_zip(path: Path) -> dict[str, str]:
    out = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            low = name.lower().replace("\\", "/")
            for kind, candidates in KNOWN_FILES.items():
                if kind not in out and low in candidates:
                    out[kind] = zf.read(name).decode("utf-8", errors="replace")
    return out


def _read_dir(path: Path) -> dict[str, str]:
    out = {}
    for kind, candidates in KNOWN_FILES.items():
        for rel in candidates:
            f = path / rel
            if f.is_file():
                out[kind] = f.read_text(encoding="utf-8", errors="replace")
                break
    return out


def parse_js_array(text: str) -> list:
    """剥掉 window.YTD.xxx.part0 = 前缀,取第一个 [ 到最后一个 ] 之间的 JSON。"""
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    return json.loads(text[start : end + 1])


def _pick(d: dict, *names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def _collect(obj, keys: set, out: list | None = None):
    """递归收集指定 key 的值(保序去重)。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, int)) and v not in out:
                out.append(v)
            else:
                _collect(v, keys, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect(item, keys, out)
    return out


def _find_nested(obj, key):
    """递归查找第一个含指定 key 的 dict 的 value。"""
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], dict):
            return obj[key]
        for v in obj.values():
            r = _find_nested(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_nested(item, key)
            if r is not None:
                return r
    return None


def _media_of(inner: dict) -> list[dict]:
    media = []
    for src in (_find_nested(inner, "media"), _find_nested(inner, "extended_entities")):
        if not src:
            continue
        for m in src.values() if isinstance(src, dict) else []:
            if not isinstance(m, list):
                continue
            for item in m:
                if isinstance(item, dict) and item.get("media_url_https"):
                    media.append({
                        "type": item.get("type", "photo"),
                        "url": item["media_url_https"],
                    })
    if not media:  # 退化:递归找图片地址
        for u in _collect(inner, {"media_url_https", "media_url"}):
            media.append({"type": "photo", "url": u})
    # 去重
    seen, uniq = set(), []
    for m in media:
        if m["url"] not in seen:
            seen.add(m["url"])
            uniq.append(m)
    return uniq


def _urls_of(inner: dict, media: list[dict]) -> list[str]:
    media_urls = {m["url"] for m in media}
    urls = []
    for u in _collect(inner, {"expanded_url"}):
        if not isinstance(u, str):
            u = str(u)
        if u in media_urls or "twimg.com" in u:
            continue
        if u not in urls:
            urls.append(u)
    return urls


def normalize_entry(obj: dict, bookmarked_at=None) -> dict:
    """把存档条目(或 tweets.js 条目)统一成 importer 需要的 dict。"""
    inner = obj.get("bookmark") or obj.get("tweet")
    if not isinstance(inner, dict):
        inner = obj
    tweet_id = _pick(inner, "tweet_id", "id", "id_str")
    if tweet_id is not None:
        tweet_id = str(tweet_id)
    text = _pick(inner, "full_text", "text", "tweet_text") or ""
    quoted = _find_nested(inner, "quoted_status") or _find_nested(inner, "quoted_status_result")
    quoted_text = ""
    if quoted:
        quoted_text = _pick(quoted, "full_text", "text") or ""
    ba = bookmarked_at or _pick(inner, "bookmarked_at", "createdAt")
    created = _pick(inner, "created_at")
    media = _media_of(inner)
    return {
        "tweet_id": tweet_id,
        "text": textproc.clean_text(str(text)),
        "author_handle": _pick(inner, "screen_name", "username"),
        "author_name": _pick(inner, "name"),
        "created_at": textproc.to_iso_utc(created),
        "bookmarked_at": textproc.to_iso_utc(ba),
        "lang": _pick(inner, "language", "lang"),
        "media": media,
        "urls": _urls_of(inner, media),
        "is_retweet": bool(str(text).startswith("RT @") or _find_nested(inner, "retweeted_status")),
        "is_reply": bool(_pick(inner, "in_reply_to_status_id", "in_reply_to_status_id_str", "in_reply_to_tweet_id")),
        "quoted_text": textproc.clean_text(str(quoted_text)),
        "raw": obj,
    }


@dataclass
class ArchiveResult:
    rows: list[dict] = field(default_factory=list)
    no_content: int = 0          # 只有 id 没有正文的条目
    joined_from_tweets: int = 0  # 从 tweets.js 补上正文的条目
    source_file: str = ""


def load_archive(path: str | Path) -> ArchiveResult:
    p = Path(path)
    files = _read_zip(p) if p.is_file() else _read_dir(p)
    if "bookmarks" not in files:
        raise FileNotFoundError(f"未找到 data/bookmarks.json|js(检查路径: {p})")
    entries = parse_js_array(files["bookmarks"])
    result = ArchiveResult(source_file="bookmarks." + ("json" if '"bookmark"' in files["bookmarks"][:2000] or True else "js"))

    # tweets.js 备查(处理"只有 id"的条目;它只含本人推文,能补多少算多少)
    own_tweets = {}
    if "tweets" in files:
        for obj in parse_js_array(files["tweets"]):
            norm = normalize_entry(obj)
            if norm["tweet_id"] and norm["text"]:
                own_tweets[norm["tweet_id"]] = obj

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        norm = normalize_entry(entry)
        if not norm["tweet_id"]:
            continue
        if not norm["text"]:
            own = own_tweets.get(norm["tweet_id"])
            if own is not None:
                norm = normalize_entry(own, bookmarked_at=norm["bookmarked_at"])
                result.joined_from_tweets += 1
            else:
                result.no_content += 1
                continue
        result.rows.append(norm)
    return result
