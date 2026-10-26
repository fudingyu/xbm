import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np

from xbm import (auto, category, cluster, db, embed, importer, projection,
                 report, search, sync, textproc, threads, timeline)


def make_archive_zip(path: Path, style: str):
    """生成合成存档 ZIP:js_full(旧版带全文)/ json_flat(新版扁平)/ id_only(只有 id)。"""
    js_full = [
        {"bookmark": {
            "bookmarked_at": "2025-03-01T10:00:00.000Z",
            "id": "1001",
            "full_text": "人工智能大模型的推理成本正在快速下降 https://t.co/abc",
            "created_at": "Sat Feb 25 12:00:00 +0000 2025",
            "screen_name": "ai_news", "name": "AI 新闻", "language": "zh",
            "entities": {"urls": [{"expanded_url": "https://blog.example.com/inference-cost"}]},
        }},
        {"bookmark": {  # 同一条推文书签两次,更早的时间应被保留
            "bookmarked_at": "2025-02-01T08:00:00.000Z",
            "id": "1001",
            "full_text": "人工智能大模型的推理成本正在快速下降 https://t.co/abc",
            "created_at": "Sat Feb 25 12:00:00 +0000 2025",
            "screen_name": "ai_news", "name": "AI 新闻", "language": "zh",
        }},
        {"bookmark": {
            "bookmarked_at": "2025-03-02T10:00:00.000Z",
            "id": "1002",
            "full_text": "CPU inference optimization thread for small models https://t.co/x",
            "created_at": "Sun Mar 01 09:30:00 +0000 2025",
            "screen_name": "llm_guy", "name": "LLM Guy", "language": "en",
        }},
        {"bookmark": {
            "bookmarked_at": "2025-03-03T10:00:00.000Z",
            "id": "1003",
            "full_text": "RT @someone: 转发一条关于向量数据库的对比测评",
            "created_at": "Mon Mar 02 18:00:00 +0000 2025",
            "screen_name": "reblogger", "language": "zh",
        }},
        {"bookmark": {
            "bookmarked_at": "2025-03-04T10:00:00.000Z",
            "id": "1004",
            "full_text": "这张架构图讲得很清楚 https://t.co/img1",
            "created_at": "Tue Mar 03 10:00:00 +0000 2025",
            "screen_name": "arch_guy", "language": "zh",
            "entities": {"media": [
                {"media_url_https": "https://pbs.twimg.com/media/abc.jpg", "type": "photo"}]},
        }},
        {"bookmark": {
            "bookmarked_at": "2025-03-05T10:00:00.000Z",
            "id": "1005",
            "full_text": "回复:我补充一个观点",
            "in_reply_to_status_id": "999",
            "created_at": "Wed Mar 04 11:00:00 +0000 2025",
            "screen_name": "replier", "language": "zh",
        }},
    ]
    json_flat = [
        {"tweet_id": "2001", "bookmarked_at": "2025-04-01T10:00:00.000Z",
         "full_text": "京都红叶季的旅行攻略,人少景美的五个 spots",
         "created_at": "Thu Mar 20 08:00:00 +0000 2025",
         "screen_name": "traveler", "language": "zh"},
        {"tweet_id": "1001",  # 已存在:不应覆盖首见内容
         "bookmarked_at": "2025-04-02T10:00:00.000Z",
         "full_text": "被改过的文本", "screen_name": "attacker", "language": "zh"},
    ]
    id_only = [{"bookmark": {"bookmarked_at": f"2025-05-0{i}T10:00:00.000Z", "tweet_id": f"300{i}"}}
               for i in range(1, 6)]
    own_tweets = [{"tweet": {
        "id": "3001", "full_text": "我自己写的第一条推文",
        "created_at": "Fri May 01 09:00:00 +0000 2025", "screen_name": "me"}},
        {"tweet": {
            "id": "3002", "full_text": "我自己写的第二条推文",
            "created_at": "Sat May 02 09:00:00 +0000 2025", "screen_name": "me"}}]

    with zipfile.ZipFile(path, "w") as zf:
        if style == "js_full":
            zf.writestr("data/bookmarks.js",
                        "window.YTD.bookmarks.part0 = " + json.dumps(js_full, ensure_ascii=False))
        elif style == "json_flat":
            zf.writestr("data/bookmarks.json", json.dumps(json_flat, ensure_ascii=False))
        elif style == "id_only":
            zf.writestr("data/bookmarks.js",
                        "window.YTD.bookmarks.part0 = " + json.dumps(id_only, ensure_ascii=False))
            zf.writestr("data/tweets.js",
                        "window.YTD.tweets.part0 = " + json.dumps(own_tweets, ensure_ascii=False))
        else:
            raise ValueError(style)


class FakeClient(embed.EmbedClient):
    """关键词位图向量:包含对应关键词的文本在该维度为 1,归一化后余弦可预期。"""

    KEYS = ["人工智能 大模型", "红叶 京都", "inference optimization"]

    def embed(self, texts):
        return np.array([self._one(t) for t in texts], dtype=np.float32)

    def _one(self, t):
        v = np.zeros(len(self.KEYS) + 1, dtype=np.float32)
        v[-1] = 0.2
        for i, keys in enumerate(self.KEYS):
            if any(k in t for k in keys.split()):
                v[i] = 1.0
        return v / np.linalg.norm(v)


class TestTextproc(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(textproc.clean_text("看这个 https://t.co/ab12 好"), "看这个 好")
        self.assertEqual(  # 保留换行、折叠行内空白
            textproc.clean_text("第一行 https://t.co/x\n第二行  多  空格\n\n第三行"),
            "第一行\n第二行 多 空格\n第三行")
        self.assertEqual(textproc.detect_lang("人工智能推理"), "zh")
        self.assertEqual(textproc.detect_lang("plain english text"), "en")
        self.assertEqual(
            textproc.to_iso_utc("Sat Feb 25 12:00:00 +0000 2025"), "2025-02-25 12:00:00")
        self.assertEqual(textproc.to_iso_utc("2025-03-01T10:00:00.000Z"), "2025-03-01 10:00:00")


class TestArchiveImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = db.connect(Path(self.tmp.name) / "t.db")
        self.zip_path = Path(self.tmp.name) / "arch.zip"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_js_full(self):
        make_archive_zip(self.zip_path, "js_full")
        st = importer.import_archive(self.db, self.zip_path)
        self.assertEqual(st.tweets_new, 5)       # 6 条中 1 条重复
        self.assertEqual(st.tweets_dup, 1)
        # 更早的书签时间被保留
        ba = self.db.execute(
            "SELECT bookmarked_at FROM bookmark WHERE tweet_id='1001'").fetchone()[0]
        self.assertEqual(ba, "2025-02-01 08:00:00")
        # t.co 已清洗,FTS 中文子串可查
        n = self.db.execute(
            "SELECT COUNT(*) FROM tweet_fts WHERE tweet_fts MATCH '\"推理成本\"'").fetchone()[0]
        self.assertEqual(n, 1)
        # 媒体/回复标记
        media = self.db.execute(
            "SELECT json_array_length(media_json) FROM tweet WHERE tweet_id='1004'").fetchone()[0]
        self.assertEqual(media, 1)
        reply = self.db.execute(
            "SELECT is_reply FROM tweet WHERE tweet_id='1005'").fetchone()[0]
        self.assertEqual(reply, 1)

    def test_json_flat_no_overwrite(self):
        make_archive_zip(self.zip_path, "js_full")
        importer.import_archive(self.db, self.zip_path)
        self.zip_path.unlink()
        make_archive_zip(self.zip_path, "json_flat")
        st = importer.import_archive(self.db, self.zip_path)
        self.assertEqual(st.tweets_new, 1)
        self.assertEqual(st.tweets_dup, 1)
        text = self.db.execute("SELECT text FROM tweet WHERE tweet_id='1001'").fetchone()[0]
        self.assertIn("推理成本", text)  # 首见内容未被"改过的文本"覆盖

    def test_id_only_join(self):
        make_archive_zip(self.zip_path, "id_only")
        st = importer.import_archive(self.db, self.zip_path)
        self.assertEqual(st.joined_from_tweets, 2)
        self.assertEqual(st.no_content, 3)
        self.assertEqual(st.tweets_new, 2)


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = db.connect(Path(self.tmp.name) / "t.db")
        zip_path = Path(self.tmp.name) / "arch.zip"
        make_archive_zip(zip_path, "js_full")
        make_archive_zip(Path(self.tmp.name) / "flat.zip", "json_flat")
        importer.import_archive(self.db, zip_path)
        importer.import_archive(self.db, Path(self.tmp.name) / "flat.zip")
        embed.ensure_embeddings(self.db, FakeClient())

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _qvec(self, query):
        return FakeClient().embed([query])[0].tolist()

    def test_vector(self):
        hits, note = search.hybrid_search(self.db, "大模型 成本", qvec=self._qvec("人工智能 成本"), mode="vector")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["tweet_id"], "1001")
        self.assertGreater(hits[0]["sim"], 0.5)

    def test_fts_chinese(self):
        hits, _ = search.hybrid_search(self.db, "旅行攻略", qvec=None, mode="fts")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["tweet_id"], "2001")
        self.assertTrue(hits[0]["fts_hit"])

    def test_hybrid_and_filters(self):
        hits, _ = search.hybrid_search(
            self.db, "推理", qvec=self._qvec("推理 优化"), mode="auto",
            author="ai_news")
        self.assertTrue(hits)
        self.assertTrue(all("ai_news" == h["author_handle"] for h in hits))

    def test_report(self):
        text = report.build_report(self.db)
        self.assertIn("Top 作者", text)
        self.assertIn("嵌入覆盖: 6/6", text)


def gql_page(tweet_ids, cursor="NEXT"):
    entries = []
    for tid in tweet_ids:
        entries.append({
            "entryId": f"tweet-{tid}",
            "content": {"itemContent": {"tweet_result": {"result": {
                "__typename": "Tweet",
                "rest_id": tid,
                "legacy": {
                    "full_text": f"推文{tid}:关于本地推理的讨论 https://t.co/z",
                    "created_at": "Sat Jan 14 22:10:03 +0000 2025",
                    "lang": "zh",
                    "entities": {"urls": [{"expanded_url": "https://example.com/p/1"}]},
                },
                "core": {"user_results": {"result": {"legacy": {
                    "screen_name": "gql_user", "name": "GQL User"}}}},
            }}}},
        })
    entries.append({"entryId": "cursor-bottom-0",
                    "content": {"itemContent": {"value": cursor}}})
    return {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [{"entries": entries}]}}}}


class TestSyncParsing(unittest.TestCase):
    def test_extract_and_normalize(self):
        payload = gql_page(["7001", "7002"])
        tweets, cursor = sync.extract_page(payload)
        self.assertEqual(cursor, "NEXT")
        self.assertEqual(len(tweets), 2)
        t = tweets[0]
        self.assertEqual(t["tweet_id"], "7001")
        self.assertEqual(t["author_handle"], "gql_user")
        self.assertEqual(t["created_at"], "2025-01-14 22:10:03")
        self.assertEqual(t["urls"], ["https://example.com/p/1"])
        self.assertIn("本地推理", t["text"])
        self.assertNotIn("t.co", t["text"])

    def test_wrapped_visibility_tweet(self):
        wrapped = gql_page(["7001"])["data"]["bookmark_timeline_v2"]["timeline"]["instructions"][0]["entries"][0]
        wrapped["content"]["itemContent"]["tweet_result"]["result"] = {
            "__typename": "TweetWithVisibilityResults",
            "tweet": wrapped["content"]["itemContent"]["tweet_result"]["result"],
        }
        tweets, _ = sync.extract_page(
            {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [
                {"entries": [wrapped, {"entryId": "cursor-bottom-0",
                                       "content": {"itemContent": {"value": "C"}}}]}]}}}})
        self.assertEqual(len(tweets), 1)
        self.assertEqual(tweets[0]["tweet_id"], "7001")


class TestSyncTwscrape(unittest.TestCase):
    """用 duck-typed 假 API 测 twscrape 后端逻辑(不依赖真实包/网络)。"""

    class FakeTweet:
        def __init__(self, tid, text):
            self._d = {
                "id": tid, "rawContent": text,
                "date": "2025-06-01T12:00:00+00:00", "lang": "zh",
                "user": {"username": "tws_user", "displayname": "Tws"},
                "photos": ["https://pbs.twimg.com/media/p1.jpg"],
                "videos": [], "outlinks": ["https://example.com/x"],
                "quotedTweet": {"rawContent": "被引用的推文"},
                "retweetedTweet": None, "inReplyToTweetId": None,
            }

        def dict(self):
            return self._d

    class FakeApi:
        def __init__(self, tweets):
            self.tweets = tweets

        def bookmarks(self, limit=None):
            async def gen():
                for t in self.tweets:
                    yield t
            return gen()  # 异步生成器自带 __aiter__/__anext__/aclose

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_incremental_stop(self):
        make_archive_zip(Path(self.tmp.name) / "a.zip", "js_full")
        importer.import_archive(self.db, Path(self.tmp.name) / "a.zip")  # 1001..1005 已知
        api = self.FakeApi([
            self.FakeTweet("9001", "一条新书签"),
            self.FakeTweet("9002", "另一条新书签"),
            self.FakeTweet("1001", "已知旧书签,应在此停"),
        ])
        st = sync.sync_twscrape(self.db, full=False, api=api)
        self.assertEqual(st.new_tweets, 2)
        self.assertIn("同步点", st.stopped_reason)
        row = self.db.execute("SELECT text FROM tweet WHERE tweet_id='9001'").fetchone()
        self.assertEqual(row[0], "一条新书签")

    def test_row_mapping(self):
        api = self.FakeApi([self.FakeTweet("9001", "书签文本")])
        st = sync.sync_twscrape(self.db, full=True, api=api)
        self.assertEqual(st.new_tweets, 1)
        r = self.db.execute(
            "SELECT author_handle, created_at, lang, json_array_length(media_json), "
            "json_array_length(urls_json) FROM tweet WHERE tweet_id='9001'").fetchone()
        self.assertEqual(r["author_handle"], "tws_user")
        self.assertEqual(r["created_at"], "2025-06-01 12:00:00")
        self.assertEqual(r["lang"], "zh")
        self.assertEqual(r[3], 1)  # 一张图
        self.assertEqual(r[4], 1)  # 一条外链
        embed_text = self.db.execute(
            "SELECT embed_text FROM tweet WHERE tweet_id='9001'").fetchone()[0]
        self.assertIn("被引用的推文", embed_text)  # 引用推文拼进了嵌入文本

    def test_newline_preserved_and_raw_saved(self):
        api = self.FakeApi([self.FakeTweet("9100", "标题行\n要点 1\n要点 2")])
        sync.sync_twscrape(self.db, full=True, api=api)
        text = self.db.execute("SELECT text FROM tweet WHERE tweet_id='9100'").fetchone()[0]
        self.assertIn("\n", text)                       # 原始换行保留
        embed = self.db.execute("SELECT embed_text FROM tweet WHERE tweet_id='9100'").fetchone()[0]
        self.assertNotIn("\n", embed)                   # 嵌入文本换行折叠
        raw = self.db.execute("SELECT raw_json FROM tweet WHERE tweet_id='9100'").fetchone()[0]
        self.assertIn("rawContent", raw)                # 原文已存 raw 备未来重清洗

    def test_mirror_delete_on_completed_full_walk(self):
        keep = sync.tweet_obj_to_row(self.FakeTweet("8001", "保留的书签"))
        gone = sync.tweet_obj_to_row(self.FakeTweet("8002", "X 端已取消的书签"))
        importer.import_rows(self.db, [keep, gone], "graphql")
        api = self.FakeApi([self.FakeTweet("8001", "保留的书签")])  # 全量走完只见 8001
        st = sync.sync_twscrape(self.db, full=True, api=api)         # fetched(1) < limit → 自然结束
        self.assertEqual(st.removed, 1)
        remaining = {r[0] for r in self.db.execute("SELECT tweet_id FROM bookmark")}
        self.assertEqual(remaining, {"8001"})
        tweets = {r[0] for r in self.db.execute("SELECT tweet_id FROM tweet")}
        self.assertEqual(tweets, {"8001"})


class TestCategoryCluster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = db.connect(Path(self.tmp.name) / "t.db")
        zip_path = Path(self.tmp.name) / "a.zip"
        make_archive_zip(zip_path, "js_full")
        make_archive_zip(Path(self.tmp.name) / "f.zip", "json_flat")
        importer.import_archive(self.db, zip_path)
        importer.import_archive(self.db, Path(self.tmp.name) / "f.zip")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed_vectors(self):
        """把 6 条书签分两组埋向量:AI 组贴近 v1,旅行组贴近 v2。"""
        v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        plan = {"1001": v1, "1002": v1, "1003": v1, "2001": v2, "1004": v2, "1005": v1}
        items = [(tid, (v / np.linalg.norm(v)).tolist()) for tid, v in plan.items()]
        embed.store_embeddings(self.db, items, "fake")
        return v1, v2

    def test_assign_categories(self):
        v1, v2 = self._seed_vectors()
        st = category.assign_categories(self.db, [
            ("AI与LLM", v1.tolist()), ("旅行与美食", v2.tolist())])
        self.assertEqual(st.assigned, 6)
        got = dict(self.db.execute(
            "SELECT tweet_id, category FROM tweet WHERE category IS NOT NULL"))
        self.assertEqual(got["1001"], "AI与LLM")
        self.assertEqual(got["2001"], "旅行与美食")
        counts = category.category_counts(self.db)
        self.assertEqual({c["name"] for c in counts}, {"AI与LLM", "旅行与美食"})
        self.assertEqual(sum(c["count"] for c in counts), 6)

    def test_leader_cluster(self):
        M = np.array([[1, 0], [0.99, 0.02], [0, 1], [0.01, 0.99]], dtype=np.float32)
        labels = cluster.leader_cluster(M, threshold=0.9)
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(labels[2], labels[3])
        self.assertNotEqual(labels[0], labels[2])

    def test_run_clustering_and_keyword_naming(self):
        self._seed_vectors()
        st = cluster.run_clustering(self.db, set_name="keyword",
                                    min_cluster_size=2, method="leader")
        self.assertEqual(st.tweets, 6)
        self.assertGreaterEqual(st.clusters, 2)
        named, used = cluster.name_clusters(self.db, set_name="keyword")
        self.assertEqual(named, st.clusters)
        self.assertEqual(used, "keyword")
        sets = cluster.cluster_list(self.db)
        kws = sets["keyword"]
        self.assertTrue(all(c["name"] for c in kws))
        names = " ".join(c["name"] for c in kws).lower()
        self.assertRegex(names, "推理|向量|人工智能|inference|红叶|京都|旅行|架构")

    def test_two_partitions_independent(self):
        """关键词分区与 LLM 分区各自独立:互不覆盖、粒度可不同。"""
        self._seed_vectors()
        st1 = cluster.run_clustering(self.db, set_name="keyword", method="leader")
        cluster.name_clusters(self.db, set_name="keyword")
        before = dict(self.db.execute(
            "SELECT tweet_id, cluster_keyword FROM tweet"))
        # 生成 LLM 分区(不同阈值也无妨),关键词分区必须原样
        st2 = cluster.run_clustering(self.db, set_name="llm", method="leader")
        after = dict(self.db.execute("SELECT tweet_id, cluster_keyword FROM tweet"))
        self.assertEqual(before, after, "LLM 分区聚类不得改动关键词分区")
        llm_labels = {r[0]: r[1] for r in self.db.execute(
            "SELECT tweet_id, cluster_llm FROM tweet")}
        self.assertTrue(all(v is not None for v in llm_labels.values()))
        # 关键词分区的名字仍在,LLM 分区名字为空待补
        self.assertTrue(all(c["name"] != f"未命名#{c['cluster_id']}"
                            for c in cluster.cluster_list(self.db)["keyword"]))
        self.assertTrue(all(c["raw_name"] is None
                            for c in cluster.cluster_list(self.db)["llm"]))
        # LLM 分区无地址 → 明确报错,不落关键词名冒充
        with self.assertRaises(ValueError):
            cluster.name_clusters(self.db, set_name="llm", llm_base="")

    def test_llm_name_extraction_and_systemone(self):
        # 思考型模型:content 为空,名字在 reasoning_content 末尾
        name = cluster._extract_name({
            "content": "",
            "reasoning_content": "1. 分析任务\n所以最终定名\n本地推理部署"})
        self.assertEqual(name, "本地推理部署")
        self.assertEqual(cluster._extract_name({"content": "开源工具"}), "开源工具")
        self.assertEqual(cluster._extract_name({"content": "好的。\n名字:AI编程"}), "AI编程")
        # 小模型噪声名回退:句子、超短 ASCII、数字
        self.assertIsNone(cluster._extract_name({"content": "以下是这些工具的分类:略"}))
        self.assertIsNone(cluster._extract_name({"content": "Sp"}))
        self.assertIsNone(cluster._extract_name({"content": "10."}))
        self.assertIsNone(cluster._extract_name({"content": "这个词真的特别特别长完全超过十二个字"}))
        # JSON 输出路径:content 或 reasoning 中的 {"name":...}
        self.assertEqual(
            cluster._extract_name({"content": '{"name":"AI编程工具"}'}), "AI编程工具")
        self.assertEqual(cluster._extract_name({
            "content": "",
            "reasoning_content": '思考…最终确定 {"name":"手机摄影"} 收尾'}), "手机摄影")
        # JSON 里的名字同样受门禁约束
        self.assertIsNone(cluster._extract_name({"content": '{"name":"推文内容分类"}'}))
        # systemone 决策式选名:mock post
        def fake(url, payload, timeout=60):
            self.assertIn("criteria", payload["questions"]["name"])
            return {"answers": {"name": {"choice": "推理"}}}
        self.assertEqual(
            cluster._systemone_pick("http://x/v1", ["t"], ["推理", "开源"], post=fake), "推理")
        # 选项外的答案拒绝(防幻觉)
        fake2 = lambda url, payload, timeout=60: {"answers": {"name": {"choice": "不存在"}}}
        self.assertIsNone(
            cluster._systemone_pick("http://x/v1", ["t"], ["推理"], post=fake2))

    def test_progress_callbacks(self):
        self._seed_vectors()
        phases = []
        st = cluster.run_clustering(self.db, method="leader", on_phase=phases.append)
        self.assertTrue(phases, "on_phase 应至少被调用一次")
        seen = []

        def prog(i, total, preview):
            seen.append((i, total))

        named, used = cluster.name_clusters(self.db, on_progress=prog)
        self.assertEqual(named, st.clusters)
        self.assertEqual([i for i, _ in seen], list(range(1, st.clusters + 1)))
        self.assertEqual(used, "keyword")

    def test_threads_classification(self):
        """时间剖面分类:中断/持续/新芽/脉冲 四类边界。"""
        now = "2026-10"
        # 直接造带簇标与书签时间的行(import_rows 支持 bookmarked_at)
        def row(tid, text, cid, bm):
            return {"tweet_id": tid, "text": text, "author_handle": "t",
                    "bookmarked_at": bm, "raw": {}}
        rows = [
            # dormant:2025-01~2025-06 活跃后沉寂(语义簇 1)
            row("d1", "推理优化 A", 1, "2025-01-15 10:00:00"),
            row("d2", "推理优化 B", 1, "2025-03-15 10:00:00"),
            row("d3", "推理优化 C", 1, "2025-06-15 10:00:00"),
            row("d4", "推理优化 D", 1, "2025-06-20 10:00:00"),
            # persistent:2025-06 起持续到现在(语义簇 2)
            row("p1", "地图项目 A", 2, "2025-06-10 10:00:00"),
            row("p2", "地图项目 B", 2, "2025-09-10 10:00:00"),
            row("p3", "地图项目 C", 2, "2025-12-10 10:00:00"),
            row("p4", "地图项目 D", 2, "2026-03-10 10:00:00"),
            row("p5", "地图项目 E", 2, "2026-09-10 10:00:00"),
            # pulse:单月爆发(语义簇 3)
            row("s1", "热点事件 A", 3, "2026-05-01 10:00:00"),
            row("s2", "热点事件 B", 3, "2026-05-02 10:00:00"),
            row("s3", "热点事件 C", 3, "2026-05-03 10:00:00"),
            # emerging:仅近两月(语义簇 4)
            row("e1", "新方向 A", 4, "2026-09-20 10:00:00"),
            row("e2", "新方向 B", 4, "2026-10-05 10:00:00"),
        ]
        importer.import_rows(self.db, rows, "graphql")
        self.db.executemany(
            "UPDATE tweet SET cluster_llm=? WHERE tweet_id=?",
            [(cid, tid) for tid, cid in [
                ("d1", 1), ("d2", 1), ("d3", 1), ("d4", 1),
                ("p1", 2), ("p2", 2), ("p3", 2), ("p4", 2), ("p5", 2),
                ("s1", 3), ("s2", 3), ("s3", 3),
                ("e1", 4), ("e2", 4)]])
        self.db.commit()
        r = threads.analyze(self.db, now=now)
        cls = {}
        for k in ("dormant", "persistent", "pulse", "emerging"):
            for t in r["threads"][k]:
                cls[t["cluster_id"]] = k
        self.assertEqual(cls[1], "dormant")   # 沉寂 16 个月
        self.assertEqual(cls[2], "persistent")
        self.assertEqual(cls[3], "pulse")
        self.assertEqual(cls[4], "emerging")
        d = next(t for t in r["threads"]["dormant"] if t["cluster_id"] == 1)
        self.assertGreaterEqual(d["dormancy"], 16)
        self.assertTrue(d["spark"])           # sparkline 文本已生成

    def test_timeline_digest(self):
        """按月聚合主题摘要 + 渲染 + 缓存读写。"""
        v1, v2 = self._seed_vectors()
        category.assign_categories(self.db, [
            ("AI与LLM", v1.tolist()), ("旅行与美食", v2.tolist())])
        digest = timeline.build_digest(self.db, "month")
        self.assertTrue(digest, "至少一期")
        periods = [d["period"] for d in digest]
        self.assertIn("2025-02", periods)
        self.assertIn("2025-03", periods)
        feb = next(d for d in digest if d["period"] == "2025-02")
        self.assertEqual(feb["total"], 1)   # 1001(书签时间 2025-02-01)
        mar = next(d for d in digest if d["period"] == "2025-03")
        self.assertEqual(mar["total"], 4)   # 1002-1005
        apr = next(d for d in digest if d["period"] == "2025-04")
        self.assertEqual(apr["total"], 1)   # 2001(旅行)
        self.assertIn("旅行与美食", str(apr["cats"]))
        text = timeline.render_digest(digest)
        self.assertIn("类目:", text)
        self.assertIn("AI与LLM", text)
        # 季度聚合
        q = timeline.build_digest(self.db, "quarter")
        self.assertTrue(all(p.endswith(("Q1", "Q2", "Q3", "Q4")) for p in
                            (d["period"] for d in q if d["period"] != "未知")))
        # 缓存
        timeline.save_timeline(self.db, "month", digest, None)
        back = timeline.load_timeline(self.db, "month")
        self.assertEqual(back["unit"], "month")
        self.assertEqual(len(back["digest"]), len(digest))
        self.assertIsNone(back["narrative"])

    def test_projection_map(self):
        """PCA 兜底确定性 + 坐标归一化入库 + map_points 分组数据。"""
        self._seed_vectors()
        st = projection.compute_map(self.db, method="pca")
        self.assertEqual(st.method, "pca")
        self.assertEqual(st.points, 6)
        n = self.db.execute(
            "SELECT COUNT(*) FROM tweet WHERE map_x IS NOT NULL").fetchone()[0]
        self.assertEqual(n, 6)
        for x, y in self.db.execute(
                "SELECT map_x, map_y FROM tweet WHERE map_x IS NOT NULL"):
            self.assertTrue(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0, (x, y))
        data = projection.map_points(self.db, "category")
        self.assertEqual(len(data["points"]), 6)
        self.assertTrue(all("g" in p and "t" in p for p in data["points"]))

    def test_incremental_classification(self):
        """新书签增量归位:簇心持久化 + 自愈回填 + 类目/分区毫秒级分配。"""
        v1, v2 = self._seed_vectors()
        category.assign_categories(self.db, [
            ("AI与LLM", v1.tolist()), ("旅行与美食", v2.tolist())])
        cluster.run_clustering(self.db, set_name="keyword", method="leader")
        # 新书签 A:贴近 v1 组;B:两个方向都远(未匹配)
        importer.import_rows(self.db, [{
            "tweet_id": "9500", "text": "新书签:大模型推理优化",
            "author_handle": "new1", "raw": {},
        }, {
            "tweet_id": "9501", "text": "novel outlier topic zzz",
            "author_handle": "new2", "raw": {},
        }], "graphql")
        embed.store_embeddings(self.db, [
            ("9500", (v1 / np.linalg.norm(v1)).tolist()),
            ("9501", ([0.0, 0.0, 1.0]))], "fake")
        r = auto.classify_new(self.db)
        self.assertEqual(r["category"], 2)      # 两条都补了类目
        got = {r["tweet_id"]: r for r in self.db.execute(
            "SELECT tweet_id, category, cluster_keyword FROM tweet "
            "WHERE tweet_id IN ('9500','9501')")}
        self.assertEqual(got["9500"]["category"], "AI与LLM")
        self.assertIsNotNone(got["9500"]["cluster_keyword"])   # 贴近 → 归位
        self.assertIsNone(got["9501"]["cluster_keyword"])      # 离群 → 留空待重聚类
        # 簇心自愈:清空后 assign_missing 能回填并继续归位
        self.db.execute("UPDATE cluster SET centroid_json=NULL WHERE set_name='keyword'")
        self.db.commit()
        importer.import_rows(self.db, [{
            "tweet_id": "9502", "text": "又一条推理优化", "author_handle": "n3", "raw": {}}], "graphql")
        embed.store_embeddings(self.db, [("9502", (v1 / np.linalg.norm(v1)).tolist())], "fake")
        r2 = auto.classify_new(self.db)
        self.assertGreaterEqual(r2["kw"], 1)
        cid = self.db.execute(
            "SELECT cluster_keyword FROM tweet WHERE tweet_id='9502'").fetchone()[0]
        self.assertIsNotNone(cid)

    def test_granularity_and_naming_options(self):
        self.assertEqual(cluster.granularity_threshold("coarse"), 0.62)
        self.assertEqual(cluster.granularity_threshold("medium"), 0.65)
        self.assertEqual(cluster.granularity_threshold("unknown"), 0.65)  # 未知值回退默认
        # LLM 分区无端点 → 明确报错
        with self.assertRaises(ValueError):
            cluster.name_clusters(self.db, set_name="llm", llm_base="")

    def test_browse_query_by_category(self):
        v1, v2 = self._seed_vectors()
        category.assign_categories(self.db, [
            ("AI与LLM", v1.tolist()), ("旅行与美食", v2.tolist())])
        n = self.db.execute(
            "SELECT COUNT(*) FROM tweet WHERE category='AI与LLM'").fetchone()[0]
        self.assertEqual(n, 4)


if __name__ == "__main__":
    unittest.main()
