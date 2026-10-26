"""CLI 入口:sync / import / report / embed / search / serve。"""

import argparse
import sys

from . import __version__, config, db, embed, importer, report as report_mod, search as search_mod, sync as sync_mod


def cmd_sync(args):
    if args.login:
        sync_mod.login_wizard()
        return 0
    if args.cookie:
        creds = sync_mod.parse_cookie_string(args.cookie)
        if not creds["auth_token"] or not creds["ct0"]:
            print("cookie 串里没解析出 auth_token/ct0,检查格式(应含 auth_token=...; ct0=...)")
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
    print(f"开始增量同步(后端 {backend};遇已知推文即停,可分批多次跑)…")

    def on_progress(st):
        print(f"  进度: 取回 {st.fetched},新增 {st.new_tweets},重复 {st.dup_tweets}")

    try:
        if backend == "twscrape":
            st = sync_mod.sync_twscrape(conn, full=args.full, limit=args.limit, on_batch=on_progress)
        else:
            st = sync_mod.sync(conn, full=args.full, max_pages=args.max_pages, on_page=on_progress)
    except sync_mod.SyncError as e:
        print(f"同步失败: {e}")
        return 1
    print(f"完成:{st.fetched} 条取回,新增 {st.new_tweets},重复 {st.dup_tweets}。({st.stopped_reason})")
    if st.new_tweets:
        from . import auto
        print("自动处理新书签(嵌入 + 类目/分区归位)…")
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
            print(f"本地模型批量嵌入(litert_lm {args.backend})…")
            last = [0]

            def progress(total):
                if total - last[0] >= 512:
                    last[0] = total
                    print(f"  已嵌入 {total}")

            n = emb.ensure_embeddings_local(
                conn, embedder=emb.get_embedder(args.backend), on_batch=progress)
            print(f"完成,共嵌入 {n} 条。")
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
    print(f"完成,共嵌入 {n} 条。")
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
        print("(无结果)")
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
    ap = argparse.ArgumentParser(prog="xbm", description="X.com 书签本地管理器")
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None, help="SQLite 路径(默认 ./xbm.db,或环境变量 XBM_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="同步书签(主数据源;twscrape 后端优先)", parents=[common])
    p.add_argument("--login", action="store_true", help="交互向导:打开 x.com 引导复制 Cookie 一次")
    p.add_argument("--cookie", help='直接传整串 Cookie("auth_token=...; ct0=...; ...")或 Cookie-Editor JSON')
    p.add_argument("--auth-token", help="单独传 auth_token(需配合 --ct0)")
    p.add_argument("--ct0", help="单独传 ct0(需配合 --auth-token)")
    p.add_argument("--init", action="store_true", help="生成同步配置模板(raw 后端需手填 endpoint)")
    p.add_argument("--backend", choices=["auto", "twscrape", "raw"], default="auto",
                   help="auto=装了 twscrape 就用它,否则 raw GraphQL")
    p.add_argument("--full", action="store_true", help="翻到底(首次全量,建议分批)")
    p.add_argument("--max-pages", type=int, default=50, help="raw 后端单次页数上限")
    p.add_argument("--limit", type=int, default=500, help="twscrape 后端单次条数上限")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("import", help="导入官方存档 ZIP/目录(后备路径)", parents=[common])
    p.add_argument("--archive", required=True)
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("report", help="数据质量报告", parents=[common])
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("embed", help="批量嵌入(默认进程内 litert_lm;可选后端)", parents=[common])
    p.add_argument("--base", default=None, help="OpenAI 兼容嵌入服务地址(不填则用本地模型)")
    p.add_argument("--backend", choices=["cpu", "gpu"], default="cpu",
                   help="本地模型后端:cpu ~9条/s,gpu(WebGPU) ~15条/s")
    p.set_defaults(fn=cmd_embed)

    p = sub.add_parser("search", help="混合检索", parents=[common])
    p.add_argument("query")
    p.add_argument("--mode", choices=["auto", "fts", "vector"], default="auto")
    p.add_argument("-k", "--top", type=int, default=20)
    p.add_argument("--author")
    p.add_argument("--before", help="如 2025-01-01")
    p.add_argument("--after")
    p.add_argument("--has-media", action="store_true")
    p.add_argument("--base", default=None, help="嵌入服务地址(查询向量用)")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("serve", help="本地 Web 服务(检索 UI + 浏览器 WebGPU 嵌入)", parents=[common])
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--model", default=None, help=".litertlm 模型路径")
    p.add_argument("--web", default=None, help="前端构建目录(默认 web/dist)")
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
