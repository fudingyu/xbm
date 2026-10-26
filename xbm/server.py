"""本地 HTTP 服务:静态前端 + 数据 API + 模型文件流。

- 浏览器(主嵌入路径):GET /model 拉 .litertlm,WebGPU 建引擎;
  GET /api/pending 取未嵌入文本 → POST /api/embeddings 回传向量存 SQLite
- 查询:浏览器算查询向量,POST /api/search 做 FTS+向量 RRF 混合检索
- 同步:POST /api/sync 触发 GraphQL 增量(可能较慢,前端有等待提示)
"""

import json
import mimetypes
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import category as category_mod
from . import cluster as cluster_mod
from . import config, db, embed, report as report_mod, search as search_mod, sync as sync_mod

DEFAULT_MODEL = None  # 经 config.discover_model() 解析(./models/ 自动发现)
DEFAULT_WEB = Path(__file__).resolve().parent.parent / "web" / "dist"

def _conn(server):
    """每个请求线程一个 SQLite 连接(db_file 挂在 server 实例上,跨线程可见)。"""
    conns = getattr(server, "_thread_conns", None)
    if conns is None:
        conns = server._thread_conns = {}
    tid = threading.get_ident()
    if tid not in conns:
        conns[tid] = db.connect(server.db_file)
    return conns[tid]


def _twscrape_state() -> dict | None:
    """读 twscrape 账号库:请求计数与限速锁(后台回填进度来源)。"""
    import sqlite3
    try:
        con = sqlite3.connect("accounts.db", timeout=2)
        row = con.execute("SELECT locks, stats, last_used FROM accounts LIMIT 1").fetchone()
        if row:
            return {
                "requests": (json.loads(row[1]) if row[1] else {}).get("Bookmarks"),
                "locked_until": (json.loads(row[0]) if row[0] else {}).get("Bookmarks"),
                "last_used": row[2],
            }
    except Exception:  # noqa: BLE001 - 无库/无表时静默
        pass
    return None


_embed_job = {"running": False, "backend": None, "done": 0, "total": 0,
              "rate": None, "error": None, "last_result": None}


def _start_embed_job(db_file: str, backend: str):
    import threading
    import time

    def worker():
        conn = db.connect(db_file)
        t0 = time.time()
        try:
            emb = embed.get_embedder(backend)
            _embed_job.update(running=True, backend=backend, done=0, error=None,
                              total=conn.execute(
                                  "SELECT COUNT(*) FROM tweet "
                                  "WHERE embedding IS NULL AND embed_text != ''").fetchone()[0])

            def prog(total):
                _embed_job["done"] = total
                _embed_job["rate"] = round(total / max(time.time() - t0, 1e-9), 1)

            n = embed.ensure_embeddings_local(conn, embedder=emb, on_batch=prog)
            _embed_job["last_result"] = f"完成({backend}):新增 {n} 条"
        except Exception as e:  # noqa: BLE001
            _embed_job["error"] = f"{type(e).__name__}: {e}"
        finally:
            _embed_job["running"] = False
            conn.close()

    threading.Thread(target=worker, daemon=True, name=f"embed-{backend}").start()


_cluster_job = {"running": False, "phase": None, "done": 0, "total": 0,
                "current": None, "granularity": None, "style": None,
                "result": None, "error": None}


def _start_cluster_job(db_file: str, payload: dict):
    import threading

    def worker():
        conn = db.connect(db_file)
        try:
            set_name = payload.get("set", "keyword")
            if set_name not in ("keyword", "llm"):
                raise ValueError("set 必须是 keyword 或 llm")
            _cluster_job.update(running=True, phase="clustering", done=0, total=0,
                                current=None, result=None, error=None,
                                granularity=payload.get("granularity", "medium"),
                                style=set_name)
            cfg = config.load_llm_config()
            base = payload.get("llm_base") or cfg["base"]
            model = payload.get("llm_model") or cfg["model"]
            style = payload.get("llm_style") or cfg["style"]
            if set_name == "llm" and not base:
                raise ValueError("LLM 分区命名需要 API 地址")
            if payload.get("llm_base") and payload["llm_base"] != cfg["base"]:
                config.save_llm_config(base, model, style)

            recluster = payload.get("recluster", True)
            if recluster:
                st = cluster_mod.run_clustering(
                    conn, set_name=set_name,
                    granularity=payload.get("granularity", "medium"),
                    min_cluster_size=int(payload.get("min_size", 12)),
                    on_phase=lambda label: _cluster_job.update(
                        phase="clustering", current=label))
                total = st.clusters
            else:
                st = None
                total = conn.execute(
                    "SELECT COUNT(*) FROM cluster WHERE set_name=? AND name IS NULL",
                    (set_name,)).fetchone()[0]
                if total == 0:
                    _cluster_job["result"] = {"set": set_name, "clusters": 0,
                                              "named": 0, "note": "没有待命名的簇"}
                    _cluster_job.update(phase="done", running=False)
                    return
            _cluster_job.update(phase="naming", done=0, total=total, current=None)

            def prog(i, tot, preview):
                _cluster_job.update(done=i, total=tot, current=preview or None)

            named, _ = cluster_mod.name_clusters(
                conn, set_name=set_name, llm_base=base, llm_model=model,
                style=style, on_progress=prog)
            total_all = conn.execute(
                "SELECT COUNT(*) FROM cluster WHERE set_name=?", (set_name,)).fetchone()[0]
            _cluster_job["result"] = {
                "set": set_name, "clusters": total_all, "named": named,
                "noise": st.noise if st else None,
                "method": st.method if st else "unchanged",
                "note": f"{ '关键词' if set_name == 'keyword' else 'LLM' }分区:"
                        f"共 {total_all} 簇,本次命名 {named} 个"}
            _cluster_job.update(phase="done", running=False)
        except Exception as e:  # noqa: BLE001
            _cluster_job["error"] = f"{type(e).__name__}: {e}"
            _cluster_job.update(running=False, phase="error")
        finally:
            conn.close()

    threading.Thread(target=worker, daemon=True, name="cluster-job").start()


_sync_job = {"running": False, "phase": None, "backend": None, "fetched": 0, "new": 0,
             "reason": None, "auto": None, "error": None, "last_result": None}
_map_job = {"running": False, "phase": None, "points": 0, "method": None,
            "seconds": None, "error": None, "last_result": None}


def _start_sync_job(db_file: str, payload: dict):
    import threading

    def worker():
        conn = db.connect(db_file)
        try:
            _sync_job.update(running=True, phase="syncing", fetched=0, new=0,
                             reason=None, auto=None, error=None)
            backend = payload.get("backend", "auto")
            if backend == "auto":
                try:
                    import twscrape  # noqa: F401
                    backend = "twscrape"
                except ImportError:
                    backend = "raw"
            _sync_job["backend"] = backend

            def cb(st):
                _sync_job.update(fetched=st.fetched, new=st.new_tweets)

            if backend == "twscrape":
                st = sync_mod.sync_twscrape(
                    conn, full=bool(payload.get("full")),
                    limit=int(payload.get("limit", 500)), on_batch=cb)
            else:
                st = sync_mod.sync(conn, full=bool(payload.get("full")),
                                   max_pages=int(payload.get("max_pages", 30)),
                                   on_page=cb)
            _sync_job.update(fetched=st.fetched, new=st.new_tweets,
                             reason=st.stopped_reason)
            if st.new_tweets:
                _sync_job["phase"] = "embedding"
                from . import auto
                _sync_job["auto"] = auto.classify_new(conn)
            auto_info = _sync_job.get("auto") or {}
            _sync_job["last_result"] = (
                f"新增 {st.new_tweets}/{st.fetched}({st.stopped_reason})"
                + (f",自动分类:类目 {auto_info['category']},分区 "
                   f"{auto_info['kw'] + auto_info['llm']}" if auto_info else ""))
            _sync_job.update(phase="done", running=False)
        except sync_mod.SyncError as e:
            _sync_job["error"] = str(e)
            _sync_job.update(running=False, phase="error")
        except Exception as e:  # noqa: BLE001
            _sync_job["error"] = f"{type(e).__name__}: {e}"
            _sync_job.update(running=False, phase="error")
        finally:
            conn.close()

    threading.Thread(target=worker, daemon=True, name="sync-job").start()


def _start_map_job(db_file: str, payload: dict):
    import threading
    from . import projection

    def worker():
        conn = db.connect(db_file)
        try:
            _map_job.update(running=True, phase=None, points=0, method=None,
                            seconds=None, error=None)
            st = projection.compute_map(
                conn, method=payload.get("method", "umap"),
                on_phase=lambda label: _map_job.update(phase=label))
            if st.error:
                _map_job["error"] = st.error
                _map_job.update(running=False, phase="error")
            else:
                _map_job.update(points=st.points, method=st.method,
                                seconds=st.seconds, phase="done", running=False,
                                last_result=f"{st.method} {st.points} 点,{st.seconds}s")
        except Exception as e:  # noqa: BLE001
            _map_job["error"] = f"{type(e).__name__}: {e}"
            _map_job.update(running=False, phase="error")
        finally:
            conn.close()

    threading.Thread(target=worker, daemon=True, name="map-job").start()


_timeline_job = {"running": False, "phase": None, "unit": None,
                 "error": None, "last_result": None}


def _start_timeline_job(db_file: str, payload: dict):
    import threading
    from . import timeline as timeline_mod

    def worker():
        conn = db.connect(db_file)
        try:
            unit = payload.get("unit", "month")
            window = payload.get("window", "all")
            if unit not in ("month", "quarter"):
                raise ValueError("unit 必须是 month 或 quarter")
            if window not in ("all", "12", "24"):
                raise ValueError("window 必须是 all/12/24")
            _timeline_job.update(running=True, phase="aggregating", unit=unit,
                                 error=None, last_result=None)
            digest = timeline_mod.filter_digest(
                timeline_mod.build_digest(conn, unit), window)
            if not digest:
                raise ValueError("该时间范围内没有可统计的书签")
            _timeline_job["phase"] = "narrating"
            cfg = config.load_llm_config()
            base = payload.get("llm_base") or cfg["base"]
            model = payload.get("llm_model") or cfg["model"]
            narrative, note = None, ""
            if base:
                try:
                    narrative = timeline_mod.llm_narrative_chunked(
                        base, model,
                        timeline_mod.render_digest(digest, compact=True))
                except Exception as e:  # noqa: BLE001 - LLM 失败仍有统计可用
                    note = f"(LLM 叙事失败:{e})"
            else:
                note = "(未配置 LLM,仅统计摘要——配置后点「重新生成」补叙事)"
            timeline_mod.save_timeline(conn, unit, digest, narrative, window)
            _timeline_job.update(
                phase="done", running=False,
                last_result=f"{len(digest)} 期统计完成"
                            + (",叙事已生成" if narrative else " " + note))
        except Exception as e:  # noqa: BLE001
            _timeline_job["error"] = f"{type(e).__name__}: {e}"
            _timeline_job.update(running=False, phase="error")
        finally:
            conn.close()

    threading.Thread(target=worker, daemon=True, name="timeline-job").start()


_cards_job = {"running": False, "phase": None, "done": 0, "total": 0,
              "error": None, "last_result": None}


def _start_cards_job(db_file: str, payload: dict):
    import threading
    from . import threads as threads_mod

    def worker():
        conn = db.connect(db_file)
        try:
            _cards_job.update(running=True, phase="analyzing", done=0, total=0,
                              error=None, last_result=None)
            analysis = threads_mod.analyze(conn)
            targets = (analysis["threads"]["dormant"][:6]
                       + analysis["threads"]["persistent"][:6])
            _cards_job.update(phase="writing", total=len(targets))
            inputs = []
            for t in targets:
                inputs.append({
                    "key": f"{t['set']}:{t['cluster_id']}",
                    "prompt": threads_mod.thread_prompt(
                        t, threads_mod.thread_samples(conn, t["set"], t["cluster_id"])),
                })
            cfg = config.load_llm_config()
            base = payload.get("llm_base") or cfg["base"]
            model = payload.get("llm_model") or cfg["model"]
            if not base:
                raise ValueError("未配置 LLM API 地址(分类浏览页可配置并测试)")
            results = {}
            for i, ci in enumerate(inputs):
                results.update(threads_mod.llm_cards(base, model, [ci]))
                _cards_job.update(done=i + 1, total=len(inputs))
            old = threads_mod.load_cards(conn).get("cards", {})
            old.update(results)
            threads_mod.save_cards(conn, old)
            _cards_job.update(phase="done", running=False,
                              last_result=f"已生成 {len(results)}/{len(inputs)} 张线索卡片")
        except Exception as e:  # noqa: BLE001
            _cards_job["error"] = f"{type(e).__name__}: {e}"
            _cards_job.update(running=False, phase="error")
        finally:
            conn.close()

    threading.Thread(target=worker, daemon=True, name="cards-job").start()


def _llm_test(base: str, model: str, style: str) -> dict:
    """快速验证 LLM 配置;返回 {ok, latency_ms, detail}。"""
    import time
    if not base:
        return {"ok": False, "detail": "未填写 API 地址"}
    t0 = time.time()
    try:
        if style == "systemone":
            cluster_mod._json_post(f"{base.rstrip('/')}/systemone", {
                "state": "连接测试",
                "questions": {"ok": {"type": "noul", "instructions": "连接正常吗?"}},
            }, timeout=20)
            return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                    "detail": "SystemOne 决策接口可用"}
        # 轻量探测:只要 HTTP 200 即认为配置正确(思考型模型正文可能为空,无妨)
        data = cluster_mod._json_post(f"{base.rstrip('/')}/chat/completions", {
            "model": model, "max_tokens": 32, "temperature": 0,
            "messages": [{"role": "user", "content": "回复:OK"}],
        }, timeout=30)
        n = len(data.get("choices") or [])
        return {"ok": True, "latency_ms": int((time.time() - t0) * 1000),
                "detail": f"chat 接口可用(返回 {n} 个候选)"}
    except urllib.error.HTTPError as e:
        hint = {501: "当前模型不支持该接口(SystemOne 需 decision 模型,如 openjev/clef/granite-switch)",
                404: "路径不存在,确认地址含 /v1 且 llama-server 版本支持该接口"}.get(
                    e.code, f"HTTP {e.code}")
        return {"ok": False, "detail": hint}
    except Exception as e:  # noqa: BLE001
        return {"ok": False,
                "detail": f"无法连接({type(e).__name__}),检查服务是否启动、地址是否正确"}


class Handler(BaseHTTPRequestHandler):
    server_version = "xbm/0.1"

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, mime=None):
        if not path.is_file():
            self._json(404, {"error": f"not found: {path.name}"})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    # ---------------- GET ----------------
    def do_GET(self):
        path = self.path.split("?")[0]
        conn = _conn(self.server)
        try:
            if path in ("/", "/index.html"):
                index = self.server.web_dir / "index.html"
                if not index.is_file():
                    self._json(200, {"hint": "web/dist 不存在。发布包附带了构建好的 dist;"
                                             "从源码构建请运行 cd web && npm install && npm run build,"
                                             "或从 GitHub Release 下载预构建包解压到 web/dist。"})
                    return
                self._file(index, "text/html; charset=utf-8")
            elif path == "/model":
                self._stream_model()
            elif path == "/api/config":
                self._json(200, {
                    "model_name": config.EMBED_MODEL,
                    "query_prefix": config.QUERY_PREFIX,
                    "doc_prefix": config.DOC_PREFIX,
                    "model_ready": self.server.model_path.is_file(),
                })
            elif path == "/api/status":
                done, total = embed.coverage(conn)
                self._json(200, {
                    "total": total, "embedded": done,
                    "model": self.server.model_path.name,
                    "sync_configured": config.sync_config_path().is_file(),
                    "twscrape": _twscrape_state(),
                    "embed_job": dict(_embed_job),
                    "cluster_job": dict(_cluster_job),
                    "sync_job": dict(_sync_job),
                    "map_job": dict(_map_job),
                    "timeline_job": dict(_timeline_job),
                    "cards_job": dict(_cards_job),
                })
            elif path == "/api/pending":
                limit = min(int(self.path.split("limit=")[-1].split("&")[0]) if "limit=" in self.path else 64, 512)
                rows = embed.pending_rows(conn, limit)
                self._json(200, [{"id": r["tweet_id"],
                                  "text": config.DOC_PREFIX + (r["embed_text"] or "")}
                                 for r in rows])
            elif path == "/api/categories":
                self._json(200, {
                    "defs": [{"name": n, "desc": d} for n, d in category_mod.DEFAULT_CATEGORIES],
                    "counts": category_mod.category_counts(conn),
                    "unclassified": category_mod.unclassified(conn),
                    "llm_configured": bool(config.LLM_BASE),
                })
            elif path == "/api/clusters":
                cfg = config.load_llm_config()
                self._json(200, {
                    "clusters": cluster_mod.cluster_list(conn),
                    "options": {
                        "granularity": [{"key": k, "label": v[1]}
                                        for k, v in cluster_mod.GRANULARITY.items()],
                        "llm_base": cfg["base"], "llm_model": cfg["model"],
                        "llm_style": cfg["style"],
                    },
                })
            elif path == "/api/browse":
                self._browse(conn)
            elif path == "/api/threads":
                from . import threads as threads_mod
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                data = threads_mod.analyze(conn, now=q.get("now", [None])[0])
                data["cards"] = threads_mod.load_cards(conn).get("cards", {})
                data["cards_job"] = dict(_cards_job)
                self._json(200, data)
            elif path == "/api/timeline":
                from . import timeline as timeline_mod
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                unit = q.get("unit", ["month"])[0]
                window = q.get("window", ["all"])[0]
                self._json(200, {
                    "timeline": timeline_mod.load_timeline(conn, unit, window),
                    "job": dict(_timeline_job)})
            elif path == "/api/map/points":
                from urllib.parse import parse_qs, urlparse
                color = parse_qs(urlparse(self.path).query).get("color", ["category"])[0]
                from . import projection
                data = projection.map_points(conn, color)
                data["job"] = dict(_map_job)
                self._json(200, data)
            elif path == "/api/report":
                self._json(200, {"report": report_mod.build_report(conn)})
            else:  # 静态资源(dist 内相对路径)
                rel = path.lstrip("/")
                if ".." in rel:
                    self._json(400, {"error": "bad path"})
                else:
                    self._file(self.server.web_dir / rel)
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _browse(self, conn):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        offset = int(q.get("offset", ["0"])[0])
        limit = min(int(q.get("limit", ["50"])[0]), 200)
        conds, args, labels = [], [], []
        if q.get("category", [None])[0]:
            conds.append("t.category = ?")
            args.append(q["category"][0])
            labels.append(q["category"][0])
        if q.get("kw", [None])[0] is not None:
            conds.append("t.cluster_keyword = ?")
            args.append(int(q["kw"][0]))
        if q.get("llm", [None])[0] is not None:
            conds.append("t.cluster_llm = ?")
            args.append(int(q["llm"][0]))
        if not conds:
            self._json(400, {"error": "至少提供一个筛选维度(category/kw/llm)"})
            return
        sql = (
            "SELECT t.tweet_id, t.author_handle, t.author_name, t.text, t.created_at, "
            "       t.media_json, t.urls_json, t.category, t.cluster_keyword, t.cluster_llm, "
            "       COALESCE((SELECT bookmarked_at FROM bookmark b WHERE b.tweet_id=t.tweet_id), "
            "                t.created_at) AS date_sort, COUNT(*) OVER() AS total "
            f"FROM tweet t WHERE {' AND '.join(conds)} "
            "ORDER BY date_sort DESC LIMIT ? OFFSET ?"
        )
        rows = [dict(r) for r in conn.execute(sql, [*args, limit + 1, offset])]
        total = rows[0]["total"] if rows else 0
        has_more = len(rows) > limit
        for h in rows[:limit]:
            h["media"] = json.loads(h.pop("media_json") or "[]")
            h["urls"] = json.loads(h.pop("urls_json") or "[]")
        self._json(200, {"hits": rows[:limit], "has_more": has_more, "total": total})

    def _stream_model(self):
        p = self.server.model_path
        if not p.is_file():
            self._json(404, {"error": f"模型文件不存在: {p}。"
                                      "请把 .litertlm 放入 ./models/ 或设置 XBM_MODEL_PATH"
                                      "(详见 models/PUT-MODEL-HERE.txt)。"})
            return
        size = p.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with p.open("rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)

    # ---------------- POST ----------------
    def do_POST(self):
        path = self.path.split("?")[0]
        conn = _conn(self.server)
        try:
            if path == "/api/embeddings":
                payload = self._body()
                stored = embed.store_embeddings(
                    conn, [(v["id"], v["vec"]) for v in payload.get("vectors", [])],
                    payload.get("model", config.EMBED_MODEL))
                self._json(200, {"stored": stored})
            elif path == "/api/search":
                payload = self._body()
                f = payload.get("filters") or {}
                hits, note = search_mod.hybrid_search(
                    conn, payload.get("q", ""), qvec=payload.get("qvec"),
                    mode=payload.get("mode", "auto"), k=int(payload.get("k", 50)),
                    author=f.get("author"), before=f.get("before"),
                    after=f.get("after"), has_media=f.get("has_media"))
                for h in hits:
                    h["media"] = json.loads(h.get("media_json") or "[]")
                    h["urls"] = json.loads(h.get("urls_json") or "[]")
                self._json(200, {"hits": hits, "note": note})
            elif path == "/api/embed/run":
                payload = self._body()
                backend = payload.get("backend", "cpu")
                if backend not in ("cpu", "gpu"):
                    self._json(400, {"error": "backend 必须是 cpu 或 gpu"})
                    return
                if _embed_job["running"]:
                    self._json(409, {"error": f"嵌入任务进行中({_embed_job['backend']}):"
                                               f"{_embed_job['done']}/{_embed_job['total']}"})
                    return
                _start_embed_job(self.server.db_file, backend)
                self._json(200, {"started": True, "backend": backend})
            elif path == "/api/categories/embed":
                payload = self._body()
                cats = payload.get("categories") or []
                if not cats and payload.get("defs"):
                    # 服务端进程内嵌入类目描述(按用户选择的后端)
                    try:
                        emb = embed.get_embedder(payload.get("backend", "cpu"))
                        cats = [
                            {"name": d["name"], "vec": v.tolist()}
                            for d, v in zip(payload["defs"], emb.embed([d["desc"] for d in payload["defs"]]))
                        ]
                    except Exception as e:  # noqa: BLE001
                        self._json(400, {"error": f"服务端嵌入不可用({e}),请由浏览器回传向量"})
                        return
                if not cats:
                    self._json(400, {"error": "missing categories"})
                    return
                st = category_mod.assign_categories(
                    conn, [(c["name"], c["vec"]) for c in cats])
                self._json(200, {"assigned": st.assigned,
                                 "distribution": st.distribution})
            elif path == "/api/llm/config":
                payload = self._body()
                p = config.save_llm_config(payload.get("base", ""),
                                           payload.get("model", ""),
                                           payload.get("style", "chat"))
                self._json(200, {"saved": str(p)})
            elif path == "/api/llm/test":
                payload = self._body()
                cfg = config.load_llm_config()
                self._json(200, _llm_test(
                    payload.get("llm_base") or cfg["base"],
                    payload.get("llm_model") or cfg["model"],
                    payload.get("llm_style") or cfg["style"]))
            elif path == "/api/clusters/run":
                payload = self._body()
                if _cluster_job["running"]:
                    self._json(409, {"error": f"聚类进行中({_cluster_job['phase']}):"
                                               f"{_cluster_job['done']}/{_cluster_job['total']}"})
                    return
                if payload.get("set", "keyword") == "llm":
                    cfg = config.load_llm_config()
                    if not (payload.get("llm_base") or cfg["base"]):
                        self._json(400, {"error": "LLM 聚类需要 API 地址"
                                                   "(如 llama-server 的 http://127.0.0.1:8080/v1)"})
                        return
                _start_cluster_job(self.server.db_file, payload)
                self._json(200, {"started": True})
            elif path == "/api/delete":
                tid = str(self._body().get("tweet_id") or "")
                if not tid:
                    self._json(400, {"error": "missing tweet_id"})
                    return
                conn.execute("DELETE FROM bookmark WHERE tweet_id=?", (tid,))
                removed = 0
                still = conn.execute(
                    "SELECT 1 FROM bookmark WHERE tweet_id=?", (tid,)).fetchone()
                if still is None:  # 无其他来源引用才删推文本体
                    removed = conn.execute(
                        "DELETE FROM tweet WHERE tweet_id=?", (tid,)).rowcount
                conn.commit()
                self._json(200, {"removed": removed})
            elif path == "/api/sync":
                payload = self._body()
                if _sync_job["running"]:
                    self._json(409, {"error": f"同步进行中({_sync_job['phase']}):"
                                               f"新增 {_sync_job['new']}"})
                    return
                _start_sync_job(self.server.db_file, payload)
                self._json(200, {"started": True})
            elif path == "/api/timeline/run":
                payload = self._body()
                if _timeline_job["running"]:
                    self._json(409, {"error": f"脉络生成中({_timeline_job['phase']})"})
                    return
                _start_timeline_job(self.server.db_file, payload)
                self._json(200, {"started": True})
            elif path == "/api/threads/cards/run":
                payload = self._body()
                if _cards_job["running"]:
                    self._json(409, {"error": f"卡片生成中:{_cards_job['done']}/{_cards_job['total']}"})
                    return
                _start_cards_job(self.server.db_file, payload)
                self._json(200, {"started": True})
            elif path == "/api/map/run":
                payload = self._body()
                if _map_job["running"]:
                    self._json(409, {"error": f"地图生成中:{_map_job['phase']}"})
                    return
                _start_map_job(self.server.db_file, payload)
                self._json(200, {"started": True})
            else:
                self._json(404, {"error": "not found"})
        except sync_mod.SyncError as e:
            self._json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        print("[%s] %s" % (__import__("time").strftime("%H:%M:%S"), fmt % args))


def serve(db_file=None, port=8788, model_path=None, web_dir=None):
    import os
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.db_file = str(db_file or config.db_path())
    httpd.web_dir = Path(web_dir or os.environ.get("XBM_WEB_DIR") or DEFAULT_WEB)
    resolved = model_path or os.environ.get("XBM_MODEL_PATH") or config.discover_model()
    httpd.model_path = Path(resolved) if resolved else Path("models/README-PLACEHOLDER")
    print(f"xbm 服务: http://127.0.0.1:{port}")
    print(f"  数据库: {httpd.db_file}")
    print(f"  前端目录: {httpd.web_dir}(需先在 web/ 下 npm install && npm run build)")
    print(f"  模型: {httpd.model_path}")
    httpd.serve_forever()
