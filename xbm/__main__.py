"""CLI 入口:sync / import / report / embed / search / serve。"""

import argparse
import sys

from .i18n import t
from . import __version__, config, db, embed, importer, report as report_mod, search as search_mod, sync as sync_mod


def cmd_sync(args):
    if args.login:
        sync_mod.login_wizard()
        return 0
    if args.cookie:
        creds = sync_mod.parse_cookie_string(args.cookie)
        if not creds["auth_token"] or not creds["ct0"]:
            print(t("cookie 串里没解析出 auth_token/ct0,检查格式(应含 auth_token=...; ct0=...)",
      "No auth_token/ct0 parsed from cookie string; expected auth_token=...; ct0=..."))
            return 1
        print(f"已保存到 {sync_mod.save_credentials(creds['auth_token'], creds['ct0'])}")
        return 0
    if args.auth_token and args.ct0:
        print(f"已保存到 {sync_mod.save_credentials(args.auth_token, args.ct0)}")
        return 0
    if args.init:
        p = sync_mod.init_sync_config()
        print(f"已生成/复用配置模板: {p}\n推荐用向导: python -m xbm sync --login")
        return 0
    conn = db.connect(args.db)

    backend = args.backend
    if backend == "auto":
        try:
            import twscrape  # noqa: F401
            backend = "twscrape"
        except ImportError:
            backend = "raw"
    print(t(f"开始增量同步(后端 {backend};遇已知推文即停,可分批多次跑)…",
      f"Incremental sync (backend {backend}; stops at first known tweet)…"))

    def on_progress(st):
        print(f"  {t('进度: 取回', 'progress: fetched')} {st.fetched},{t('新增', 'new')} {st.new_tweets},{t('重复', 'dup')} {st.dup_tweets}")

    try:
        if backend == "twscrape":
            st = sync_mod.sync_twscrape(conn, full=args.full, limit=args.limit, on_batch=on_progress)
        else:
            st = sync_mod.sync(conn, full=args.full, max_pages=args.max_pages, on_page=on_progress)
    except sync_mod.SyncError as e:
        print(f"{t('同步失败:', 'Sync failed:')} {e}")
        return 1
    print(f"{t('完成:', 'Done:')} {t('取回', 'fetched')} {st.fetched},{t('新增', 'new')} {st.new_tweets},{t('重复', 'dup')} {st.dup_tweets}. ({st.stopped_reason})")
    if st.new_tweets:
        from . import auto
        print(t("自动处理新书签(嵌入 + 类目/分区归位)…",
      "Auto-processing new bookmarks (embed + assign categories/clusters)…"))
        r = auto.classify_new(conn)
        print(f"  嵌入 {r['embedded']} | 类目 {r['category']} | "
              f"关键词分区 {r['kw']}(未匹配 {r['kw_unmatched']}) | "
              f"LLM 分区 {r['llm']}(未匹配 {r['llm_unmatched']})"
              + (f" | 嵌入跳过:{r['embed_error']}" if r.get("embed_error") else ""))
    return 0


def cmd_import(args):
    conn = db.connect(args.db)
    st = importer.import_archive(conn, args.archive)
    print(f"存档导入:条目 {st.entries},新推文 {st.tweets_new},重复 {st.tweets_dup},"
          f"无正文跳过 {st.no_content},由 tweets.js 补全 {st.joined_from_tweets}")
    return 0


def cmd_report(args):
    conn = db.connect(args.db)
    print(report_mod.build_report(conn))
    return 0


def cmd_embed(args):
    conn = db.connect(args.db)
    if not args.base:  # 进程内 litert_lm,GPU(WebGPU)实测比 CPU 快 ~67%
        try:
            from . import embed as emb
            print(t(f"本地模型批量嵌入(litert_lm {args.backend})…",
      f"Bulk embedding with local model (litert_lm {args.backend})…"))
            last = [0]

            def progress(total):
                if total - last[0] >= 512:
                    last[0] = total
                    print(f"  已嵌入 {total}")

            n = emb.ensure_embeddings_local(
                conn, embedder=emb.get_embedder(args.backend), on_batch=progress)
            print(f"{t('完成,共嵌入', 'Done.')} {n} {t('条', 'bookmarks embedded')}.")
            return 0
        except ImportError:
            print("未装 litert_lm(pip install litert-lm),回退 OpenAI 兼容服务模式")
        except Exception as e:
            print(f"本地嵌入失败:{e}\n回退 OpenAI 兼容服务模式")
    client = embed.EmbedClient(base=args.base)
    print(f"服务端嵌入({client.base},模型 {client.model})…")
    try:
        n = embed.ensure_embeddings(conn, client)
    except RuntimeError as e:
        print(f"失败: {e}")
        return 1
    print(f"{t('完成,共嵌入', 'Done.')} {n} {t('条', 'bookmarks embedded')}.")
    return 0


def cmd_search(args):
    conn = db.connect(args.db)
    qvec, note = None, ""
    if args.mode != "fts":
        try:
            client = embed.EmbedClient(base=args.base)
            vec = client.embed([search_mod.embed_query_text(args.query)])[0]
            qvec = vec.tolist()
        except RuntimeError as e:
            note = f"查询向量获取失败({e}),降级为关键词检索"
    hits, note2 = search_mod.hybrid_search(
        conn, args.query, qvec=qvec, mode=args.mode, k=args.top,
        author=args.author, before=args.before, after=args.after, has_media=args.has_media)
    print(note or note2 or "")
    if not hits:
        print(t("(无结果)", "(no results)"))
        return 0
    from . import textproc
    for h in hits:
        badges = []
        if h["sim"] is not None:
            badges.append(f"语义 {h['sim']:.3f}")
        if h["fts_hit"]:
            badges.append("关键词✓")
        date = (h.get("date_sort") or "")[:10]
        print(f"[{' | '.join(badges)}] {date} @{h['author_handle'] or '?'}  {h['text'][:80]}")
        print(f"    {textproc.tweet_url(h['author_handle'], h['tweet_id'])}")
    return 0


def cmd_serve(args):
    from . import server
    server.serve(db_file=args.db, port=args.port, model_path=args.model, web_dir=args.web)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="xbm", description=t("X.com 书签本地管理器", "X.com bookmark manager"))
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="SQLite 路径(默认 ./xbm.db,或环境变量 XBM_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help=t("同步书签(主数据源;twscrape 后端优先)",
                                 "Sync bookmarks (primary source; twscrape backend preferred)"), parents=[common])
    p.add_argument("--login", action="store_true", help=t("交互向导:打开 x.com 引导复制 Cookie 一次",
                                                   "Interactive wizard: opens x.com to paste cookies once"))
    p.add_argument("--cookie", help=t('直接传整串 Cookie 或 Cookie-Editor JSON',
                                   'Raw cookie string or Cookie-Editor JSON'))
    p.add_argument("--auth-token", help=t("单独传 auth_token(需配合 --ct0)", "auth_token alone (pair with --ct0)"))
    p.add_argument("--ct0", help=t("单独传 ct0(需配合 --auth-token)", "ct0 alone (pair with --auth-token)"))
    p.add_argument("--init", action="store_true", help=t("生成同步配置模板(raw 后端需手填 endpoint)",
                                                  "Generate sync config template (raw backend needs endpoint)"))
    p.add_argument("--backend", choices=["auto", "twscrape", "raw"], default="auto",
                   help=t("auto=装了 twscrape 就用它,否则 raw GraphQL",
                          "auto=use twscrape if installed, else raw GraphQL"))
    p.add_argument("--full", action="store_true", help=t("翻到底(首次全量,建议分批)",
                                                  "Walk to the end (first full sync; run in batches)"))
    p.add_argument("--max-pages", type=int, default=50, help=t("raw 后端单次页数上限", "raw backend page cap per run"))
    p.add_argument("--limit", type=int, default=500, help=t("twscrape 后端单次条数上限", "twscrape backend item cap per run"))
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("import", help=t("导入官方存档 ZIP/目录(后备路径)",
                                   "Import official archive ZIP/dir (fallback path)"), parents=[common])
    p.add_argument("--archive", required=True, help=t("存档路径", "archive path"))
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("report", help=t("数据质量报告", "Data quality report"), parents=[common])
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("embed", help=t("批量嵌入(默认进程内 litert_lm;可选后端)",
                                   "Bulk embed (in-process litert_lm by default; backend optional)"), parents=[common])
    p.add_argument("--base", default=None, help=t("OpenAI 兼容嵌入服务地址(不填则用本地模型)",
                                            "OpenAI-compatible embedding endpoint (blank = local model)"))
    p.add_argument("--backend", choices=["cpu", "gpu"], default="cpu",
                   help=t("本地模型后端:cpu ~9条/s,gpu(WebGPU) ~15条/s",
                          "local backend: cpu ~9/s, gpu (WebGPU) ~15/s"))
    p.set_defaults(fn=cmd_embed)

    p = sub.add_parser("search", help=t("混合检索", "Hybrid search"), parents=[common])
    p.add_argument("query")
    p.add_argument("--mode", choices=["auto", "fts", "vector"], default="auto")
    p.add_argument("-k", "--top", type=int, default=20, help=t("返回条数", "top-k results"))
    p.add_argument("--author", help=t("作者 handle 过滤", "author handle filter"))
    p.add_argument("--before", help=t("如 2025-01-01", "e.g. 2025-01-01"))
    p.add_argument("--after", help=t("如 2025-01-01", "e.g. 2025-01-01"))
    p.add_argument("--has-media", action="store_true", help=t("仅含媒体", "media only"))
    p.add_argument("--base", default=None, help=t("嵌入服务地址(查询向量用)", "embedding endpoint (for query vector)"))
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("serve", help=t("本地 Web 服务(检索 UI + 浏览器 WebGPU 嵌入)",
                                   "Local web UI (search + in-browser WebGPU embedding)"), parents=[common])
    p.add_argument("--port", type=int, default=8788, help=t("端口", "port"))
    p.add_argument("--host", default=None,
                   help=t("监听地址(默认 127.0.0.1;0.0.0.0 对局域网开放)",
                          "bind address (default 127.0.0.1; 0.0.0.0 for LAN)"))
    p.add_argument("--model", default=None, help=t(".litertlm 模型路径", ".litertlm model path"))
    p.add_argument("--web", default=None, help=t("前端构建目录(默认 web/dist)", "web build dir (default web/dist)"))
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
