"""GraphQL 增量同步(主数据源):用浏览器 cookie 读取 x.com 书签。

- 增量:书签按时间倒序,翻页直到遇到已知推文即停(--full 才翻到底)
- 分批:--max-pages 每次跑若干页,限速友好,随时可断,下次继续
- 首次:多次运行逐渐吃完全量(每次从头翻页,跳过已知,直到见新)

凭据:python -m xbm sync --init 生成 ~/.xbm/x-sync.json 模板,
auth_token / ct0 从浏览器 DevTools(Application → Cookies)复制,长期有效;
endpoint 从 Network 面板任意 Bookmarks 请求的完整 URL 复制(queryId 会轮换)。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config, importer, textproc

# x.com web 客户端公开 Bearer(每个网页请求都携带,非用户私有;可在配置里覆盖)
DEFAULT_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8Ho81ndTD3tH1d2LO5IkM3LyDdPvrCFbM"
)

DEFAULT_FEATURES = {
    "graphql_timeline_v2_bookmark_timeline": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_v1_document_likes_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


class SyncError(RuntimeError):
    pass


def load_sync_config(path: Path | None = None) -> dict:
    p = path or config.sync_config_path()
    if not p.is_file():
        raise SyncError(
            f"缺少同步配置 {p}。先运行: python -m xbm sync --init ,"
            "再按文件内注释填入 auth_token / ct0(endpoint 仅 raw 后端需要)。"
        )
    cfg = json.loads(p.read_text(encoding="utf-8"))
    missing = [k for k in ("auth_token", "ct0") if not cfg.get(k)]
    if missing:
        raise SyncError(f"同步配置 {p} 缺少字段: {', '.join(missing)}")
    return cfg


def init_sync_config() -> Path:
    p = config.sync_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return p
    p.write_text(json.dumps({
        "_说明": "auth_token/ct0 从 x.com 页面 DevTools → Application → Cookies 复制;"
                 "endpoint 从 Network 面板任一 Bookmarks 请求的完整 URL 复制(含 queryId);"
                 "features 如报错按响应提示增改。",
        "auth_token": "",
        "ct0": "",
        "endpoint": "https://x.com/i/api/graphql/XXXXXXXX/Bookmarks",
        "bearer": DEFAULT_BEARER,
        "features": DEFAULT_FEATURES,
        "rate_limit_seconds": 1.5,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def parse_cookie_string(s: str) -> dict:
    """解析整串 Cookie 头("k=v; k2=v2")或 Cookie-Editor 扩展导出的 JSON。"""
    s = s.strip()
    if s.startswith("["):
        jar = {i.get("name"): i.get("value", "") for i in json.loads(s) if isinstance(i, dict)}
    else:
        jar = {}
        for part in s.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                jar[k.strip()] = v.strip()
    return {"auth_token": jar.get("auth_token", ""), "ct0": jar.get("ct0", "")}


def save_credentials(auth_token: str, ct0: str, path: Path | None = None) -> Path:
    """把凭据合并进现有配置(保留 bearer/features/rate_limit 等字段)。"""
    p = path or config.sync_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    cfg.update({"auth_token": auth_token, "ct0": ct0})
    cfg.setdefault("_说明", "auth_token/ct0 由 --login 向导写入;endpoint 仅 raw 后端需要。")
    cfg.setdefault("bearer", DEFAULT_BEARER)
    cfg.setdefault("features", DEFAULT_FEATURES)
    cfg.setdefault("rate_limit_seconds", 1.5)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def login_wizard() -> Path:
    """交互式导入浏览器凭据:打开 x.com,引导复制一次 Cookie 即可长期增量。"""
    import webbrowser

    print("== x.com 凭据导入向导 ==")
    print("auth_token 是 HttpOnly cookie,页面脚本读不到,需从 DevTools 复制一次(有效期数月)。")
    print("已为你打开 x.com 书签页(需已在浏览器登录):")
    webbrowser.open("https://x.com/i/bookmarks")
    print(
        "在 x.com 页面按 F12 → 任选以下一种方式复制:\n"
        "  A. Network 面板:刷新页面,点任意 x.com/i/api/... 请求 → Request Headers →\n"
        "     复制整行 Cookie 值(auth_token=...; ct0=...; ... 整串粘贴即可)\n"
        "  B. Application 面板 → Cookies → https://x.com:分别复制 auth_token 和 ct0 的值\n"
        "  C. 装了 Cookie-Editor 扩展:Export → JSON,整段粘贴\n"
    )
    raw = input("粘贴 Cookie 串 / auth_token 值(方式 B 直接贴 auth_token,之后会再问 ct0):\n> ").strip()
    if "=" in raw or raw.startswith("["):
        creds = parse_cookie_string(raw)
        auth_token, ct0 = creds["auth_token"], creds["ct0"]
        if auth_token and not ct0:
            ct0 = input("再粘贴 ct0 的值:\n> ").strip()
    else:
        auth_token = raw
        ct0 = input("再粘贴 ct0 的值:\n> ").strip()
    if not auth_token or not ct0:
        raise SyncError("auth_token / ct0 缺失,未保存。重新运行 xbm sync --login。")
    p = save_credentials(auth_token, ct0)
    print(f"已保存到 {p}\n现在可以运行: python -m xbm sync(增量拉取书签)")
    return p


def _fetch_page(cfg: dict, cursor: str | None) -> dict:
    if not cfg.get("endpoint") or "XXXX" in cfg["endpoint"]:
        raise SyncError("配置缺 endpoint(raw 后端需要):从浏览器 Network 面板复制 Bookmarks 请求完整 URL;"
                        "或改用 twscrape 后端(pip install twscrape),无需 endpoint。")
    variables: dict = {"count": 20, "includePromotedContent": False}
    if cursor:
        variables["cursor"] = cursor
    params = urllib.parse.urlencode({
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(cfg.get("features") or DEFAULT_FEATURES, separators=(",", ":")),
    })
    req = urllib.request.Request(
        f"{cfg['endpoint']}?{params}",
        headers={
            "Authorization": f"Bearer {cfg.get('bearer') or DEFAULT_BEARER}",
            "X-CSRF-Token": cfg["ct0"],
            "Cookie": f"auth_token={cfg['auth_token']}; ct0={cfg['ct0']}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Client-Language": "en",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read(500).decode("utf-8", errors="replace")
        if e.code == 429:
            raise SyncError("触发限速(429):稍等几分钟再跑,建议调大 rate_limit_seconds。") from e
        if e.code in (401, 403):
            raise SyncError(
                f"鉴权失败({e.code}):cookie 可能过期,重新从浏览器复制 auth_token/ct0。"
            ) from e
        raise SyncError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise SyncError(f"网络错误: {e}(需要能访问 x.com,必要时开 VPN)") from e


def _walk_entries(payload: dict):
    """兼容 timeline v2 结构:instructions[].entries[] / entry。"""
    timeline = (
        payload.get("data", {}).get("bookmark_timeline_v2", {}).get("timeline")
        or payload.get("data", {}).get("bookmark_timeline", {}).get("timeline")
        or {}
    )
    for inst in timeline.get("instructions", []):
        for key in ("entries", "entry"):
            items = inst.get(key) or []
            if isinstance(items, dict):
                items = [items]
            for entry in items:
                if isinstance(entry, dict):
                    yield entry


def normalize_graphql_tweet(result: dict) -> dict | None:
    """GraphQL tweet_result.result → 统一行 dict。"""
    r = result
    if not isinstance(r, dict):
        return None
    if r.get("__typename") == "TweetWithVisibilityResults":
        r = r.get("tweet") or {}
        if not isinstance(r, dict):
            return None
    legacy = r.get("legacy") or {}
    try:
        core_user = r["core"]["user_results"]["result"]["legacy"]
        handle, name = core_user.get("screen_name"), core_user.get("name")
    except (KeyError, TypeError):
        handle, name = None, None
    media = []
    for m in ((legacy.get("extended_entities") or {}).get("media") or []):
        if m.get("media_url_https"):
            media.append({"type": m.get("type", "photo"), "url": m["media_url_https"]})
    urls = []
    for u in ((legacy.get("entities") or {}).get("urls") or []):
        exp = u.get("expanded_url")
        if exp and "twimg.com" not in exp and exp not in urls:
            urls.append(exp)
    quoted_text = ""
    try:
        quoted_text = r["quoted_status_result"]["result"]["legacy"]["full_text"]
    except (KeyError, TypeError):
        pass
    return {
        "tweet_id": str(r.get("rest_id") or legacy.get("id_str") or ""),
        "text": textproc.clean_text(legacy.get("full_text") or ""),
        "author_handle": handle,
        "author_name": name,
        "created_at": textproc.to_iso_utc(legacy.get("created_at")),
        "bookmarked_at": None,  # GraphQL 不返回书签时间,以 first_seen 近似
        "lang": legacy.get("lang"),
        "media": media,
        "urls": urls,
        "is_retweet": "retweeted_status_result" in legacy or (legacy.get("full_text") or "").startswith("RT @"),
        "is_reply": bool(legacy.get("in_reply_to_status_id_str")),
        "quoted_text": textproc.clean_text(quoted_text or ""),
        "raw": {},
    }


def extract_page(payload: dict) -> tuple[list[dict], str | None]:
    """返回 (tweets, next_cursor);解析失败的推文写入 unparsed/ 供排查。"""
    tweets, cursor = [], None
    for entry in _walk_entries(payload):
        eid = entry.get("entryId", "")
        content = entry.get("content") or {}
        if eid.startswith("cursor-bottom"):
            item = content.get("itemContent") or {}
            cursor = item.get("value") or content.get("value") or cursor
            continue
        if not eid.startswith("tweet-"):
            continue
        try:
            result = (content.get("itemContent") or {}).get("tweet_result", {}).get("result")
            norm = normalize_graphql_tweet(result) if result else None
        except Exception:
            norm = None
        if norm and norm["tweet_id"] and norm["text"]:
            tweets.append(norm)
        elif result:
            (config.unparsed_dir() / f"tweet-{int(time.time() * 1000)}.json").write_text(
                json.dumps(result, ensure_ascii=False)[:50000], encoding="utf-8")
    return tweets, cursor


@dataclass
class SyncStats:
    pages: int = 0
    fetched: int = 0
    new_tweets: int = 0
    dup_tweets: int = 0
    removed: int = 0
    stopped_reason: str = ""


def sync(conn, full: bool = False, max_pages: int = 50, config_path: Path | None = None,
         on_page=None) -> SyncStats:
    cfg = load_sync_config(config_path)
    rate = float(cfg.get("rate_limit_seconds", 1.5))
    stats = SyncStats()
    cursor = None
    while stats.pages < max_pages:
        payload = _fetch_page(cfg, cursor)
        tweets, cursor = extract_page(payload)
        stats.pages += 1
        stats.fetched += len(tweets)
        if not tweets:
            stats.stopped_reason = "无更多数据"
            break
        if not full:
            known = {
                r[0] for r in conn.execute(
                    f"SELECT tweet_id FROM tweet WHERE tweet_id IN "
                    f"({','.join('?' * len(tweets))})",
                    [t["tweet_id"] for t in tweets],
                )
            }
            if known and known == {t["tweet_id"] for t in tweets}:
                stats.stopped_reason = "增量到达上次同步点"
                break
        imp = importer.import_rows(conn, tweets, source="graphql")
        stats.new_tweets += imp.tweets_new
        stats.dup_tweets += imp.tweets_dup
        if on_page:
            on_page(stats)
        if not cursor:
            stats.stopped_reason = "已翻到底"
            break
        time.sleep(rate)
    if not stats.stopped_reason:
        stats.stopped_reason = f"达到单次页数上限({max_pages}),下次运行继续"
    return stats


# ---------------------------------------------------------------------------
# twscrape 后端(首选):社区维护 queryId/features 轮换与限速,pip install twscrape
# ---------------------------------------------------------------------------

def tweet_obj_to_row(t) -> dict:
    """twscrape 的 Tweet 对象(.dict(),SNScrape 风格)→ 统一行 dict。"""
    d = t.dict() if callable(getattr(t, "dict", None)) else dict(t)
    user = d.get("user") or {}
    media = [{"type": "photo", "url": p} for p in (d.get("photos") or []) if isinstance(p, str)]
    for v in (d.get("videos") or []):
        thumb = v.get("thumbnailUrl") if isinstance(v, dict) else getattr(v, "thumbnailUrl", "")
        media.append({"type": "video", "url": thumb or ""})
    media_urls = {m["url"] for m in media if m["url"]}
    urls = []
    for u in (d.get("outlinks") or []) + (d.get("links") or []):
        if isinstance(u, str) and u not in urls and u not in media_urls and "twimg.com" not in u:
            urls.append(u)
    qt = d.get("quotedTweet") or {}
    quoted = (qt.get("rawContent") or "") if isinstance(qt, dict) else ""
    return {
        "tweet_id": str(d.get("id") or ""),
        "text": textproc.clean_text(d.get("rawContent") or ""),
        "author_handle": user.get("username"),
        "author_name": user.get("displayname"),
        "created_at": textproc.to_iso_utc(d.get("date")),
        "bookmarked_at": None,
        "lang": d.get("lang"),
        "media": media,
        "urls": urls,
        "is_retweet": bool(d.get("retweetedTweet")),
        "is_reply": bool(d.get("inReplyToTweetId")),
        "quoted_text": textproc.clean_text(quoted),
        "raw": {"rawContent": d.get("rawContent") or ""},
    }


def _mirror_delete(conn, seen: set) -> int:
    """全量翻完后,库里 graphql 来源但这次没见到的书签 = X 端已取消 → 本地删除。"""
    rows = [r[0] for r in conn.execute("SELECT tweet_id FROM bookmark WHERE source='graphql'")]
    gone = [tid for tid in rows if tid not in seen]
    for i in range(0, len(gone), 500):  # SQLite 参数上限保护
        chunk = gone[i : i + 500]
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM bookmark WHERE tweet_id IN ({marks})", chunk)
        conn.execute(
            f"DELETE FROM tweet WHERE tweet_id IN ({marks}) "
            f"AND tweet_id NOT IN (SELECT tweet_id FROM bookmark)", chunk)
    if gone:
        conn.commit()
    return len(gone)


def sync_twscrape(conn, full: bool = False, limit: int = 500, on_batch=None, api=None):
    """用 twscrape 读取书签:增量遇已知即停;--full 时最多取 limit 条,分批落库。"""
    import asyncio
    import contextlib
    import os

    # x.com 会拦 httpx 的 TLS 指纹(403),curl_cffi 伪装浏览器指纹;装了就用
    try:
        import curl_cffi  # noqa: F401
        os.environ.setdefault("TWS_HTTP_BACKEND", "curl")
    except ImportError:
        pass

    async def run():
        nonlocal api
        if api is None:
            try:
                from twscrape import API
            except ImportError as e:
                raise SyncError(
                    "未安装 twscrape:pip install twscrape(或改用 --backend raw)") from e
            api = API()
            cfg = load_sync_config()
            cookie = f"auth_token={cfg['auth_token']}; ct0={cfg['ct0']}"
            try:
                await api.pool.add_account_cookies("xbm", cookie)
            except Exception:  # noqa: BLE001 - 同名账号已添加过则复用
                pass
        stats = SyncStats()
        known = None if full else {r[0] for r in conn.execute("SELECT tweet_id FROM tweet")}
        buf: list[dict] = []

        def flush():
            if not buf:
                return
            imp = importer.import_rows(conn, buf, "graphql")
            stats.new_tweets += imp.tweets_new
            stats.dup_tweets += imp.tweets_dup
            buf.clear()
            if on_batch:
                on_batch(stats)

        seen: set[str] = set()
        async with contextlib.aclosing(api.bookmarks(limit=limit)) as gen:
            async for t in gen:
                stats.fetched += 1
                row = tweet_obj_to_row(t)
                seen.add(row["tweet_id"])
                if not row["tweet_id"] or not row["text"]:
                    continue
                if known is not None and row["tweet_id"] in known:
                    flush()
                    stats.stopped_reason = "增量到达上次同步点"
                    return stats
                buf.append(row)
                if len(buf) >= 50:
                    flush()
        flush()
        if full and stats.fetched < limit:
            stats.removed = _mirror_delete(conn, seen)
            stats.stopped_reason = f"已翻到书签末尾({stats.fetched} 条);镜像删除 X 端已取消的书签 {stats.removed} 条"
        else:
            stats.stopped_reason = f"取满 {stats.fetched} 条(limit={limit}),书签更多则再次运行继续"
        stats.pages = (stats.fetched + 19) // 20
        return stats

    return asyncio.run(run())
