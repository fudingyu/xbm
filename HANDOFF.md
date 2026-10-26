# xbm 技术对接文档(HANDOFF)

> 面向后续接手开发的 Agent / 开发者。本文档覆盖项目的两个版本、架构差异、数据/模型/配置的
> 实际路径、启动与构建方式、测试方法、以及开发过程中踩过的全部坑。
> 最后更新:2026-10-27。

---

## 0. 项目是什么

X.com 书签的本地管理器,解决两大痛点:无语义/模糊搜索、无法按内容分类浏览。
全部数据与计算留在用户本机,绝不经过第三方服务器。

**两个版本并存**(用户拍板"英文版继续旧架构,静态版另起"):

| | 版本 A:`web/`(双语版) | 版本 B:`webapp/`(静态版) |
|---|---|---|
| 语言 | 中文/英文双语(i18n `t()`) | 英文 |
| 架构 | Python 后端 `xbm serve` 持有数据库与模型,前端是壳 | **纯静态、零后端**;数据库在浏览器 OPFS,模型在浏览器 WebGPU |
| 部署 | 本机运行 `python -m xbm serve` | 静态托管(GitHub Pages / Cloudflare Pages 均可) |
| 适用 | 单机重度用户(嵌入快 5-7 倍) | 分享给他人、零安装 |
| 仓库 | 源项目 `C:\MyData\Development\ZCode\xbm`(未建 git);发布仓 `C:\MyData\Development\ZCode\xbm-github`(main 分支,已含两版源码) | 同左,`webapp/` 目录 |

---

## 1. 目录与关键文件(实际路径)

```
C:\MyData\Development\ZCode\xbm\               ← 源项目(开发在这里)
├── xbm\                    Python 包(版本 A 后端 + 两版共用的算法参考实现)
│   ├── server.py           版本 A 本地服务(静态+API+模型流+CORS)
│   ├── sync.py             双后端同步:twscrape(首选)/ raw GraphQL;--login 向导;删除镜像
│   ├── importer.py         多源统一入库(按 tweet_id 首见去重)
│   ├── archive.py          官方存档 ZIP 容错解析(后备数据源)
│   ├── embed.py            向量存取(BLOB float32)+ LocalEmbedder(CPU/GPU 进程内嵌入)
│   ├── search.py           FTS5 trigram + 余弦 + RRF(60) 混合检索
│   ├── category.py         零样本类目(余弦+softmax,类目向量持久化 meta.category_vecs)
│   ├── cluster.py          双方法聚类:关键词分区=IDF词向量引领聚类;LLM分区=嵌入引领聚类
│   ├── threads.py          线索账本:中断/持续/新芽/脉冲 时间剖面分类 + LLM 卡片
│   ├── timeline.py         时间脉络:按月/季聚合 + LLM 分段叙事
│   ├── auto.py             增量流水线:sync 后 自动嵌入→类目/双分区归位(零 LLM)
│   ├── projection.py       UMAP(PCA 兜底)降维存 map_x/map_y
│   ├── config.py           全部路径/环境变量解析
│   ├── db.py               SQLite schema + 迁移(见 §5 坑)
│   └── __main__.py         CLI:sync/import/report/embed/search/serve
├── web\                    版本 A 前端(双语;依赖 xbm serve)
│   ├── src/main.js + i18n.js + style.css + index.html
│   └── dist\               构建产物(含 litert wasm ~109MB)
├── webapp\                 版本 B 静态前端(英文,零后端)
│   ├── src/db-worker.js    SQLite-WASM worker(opfs-sahpool + 文件双模式)+ 全部数据操作
│   ├── src/store.js        worker RPC 客户端
│   ├── src/model.js        .litertlm 加载(URL/File → WebGPU)
│   ├── src/embedder.js     批量嵌入循环(batch 16,实测最优)
│   ├── src/search.js       余弦+子串 RRF(Python search.py 的 JS 移植)
│   ├── src/collector.js    Bookmarklet 采集器源码生成(x.com 页内执行)
│   ├── src/storage.js      存储位置管理(OPFS / 真实 .db 文件,File System Access)
│   ├── src/main.js         UI 接线(Search/Collect/Data/Settings 四视图)
│   ├── snapshot-store\     xbm.db 的持久快照(190MB,build 清 dist 后从这里恢复)
│   └── dist\               构建产物(含 litert wasm + sqlite wasm)
├── tests\test_all.py       27 项单测(版本 A 全链路)
├── accounts.db             ⚠ twscrape 账号库(含用户 cookie,已 gitignore,绝不提交)
├── xbm.db                  ⚠ 版本 A 数据库(14441 条真实书签+向量,190MB,已 gitignore)
└── README.md               中文主文档

C:\MyData\Development\ZCode\xbm-github\      ← 发布仓(git main,干净无个人数据)
    同构;个人数据/大文件全部 gitignore;静态版/双语版/对接文档都已提交
```

---

## 2. 数据库与模型路径(实际值)

| 项 | 路径 |
|---|---|
| 版本 A 数据库 | `C:\MyData\Development\ZCode\xbm\xbm.db`(14441 条,190MB,WAL 模式) |
| 版本 A 模型 | `C:\Users\fdy10\Downloads\LLM\LiteRTLM\embeddinggemma-2-text-270m.litertlm`(160MB;经 `XBM_MODEL_PATH` 指定;代码默认自动发现 `./models/*.litertlm`) |
| 版本 A 同步凭据 | `C:\Users\fdy10\.xbm\x-sync.json`(auth_token/ct0,**机密**,不在仓库) |
| 版本 A LLM 配置 | `C:\Users\fdy10\.xbm\llm.json`(base/model/style,chat 或 systemone) |
| 版本 A twscrape 账号 | `xbm\accounts.db`(项目根,**含 cookie**,已 gitignore) |
| 版本 B 数据库 | 浏览器 OPFS,origin `http://127.0.0.1:9999` 下 `.opfs-sahpool/xbm.sqlite3`(当前已导入真实 14441 条;换 origin/浏览器则各自独立) |
| 版本 B 文件句柄 | IndexedDB `xbm-fs` 库 `db-file-handle` 键(File System Access handle) |
| 版本 B LLM 配置 | localStorage `xbm-llm-base` / `xbm-llm-model` |
| 快照(测试用) | `webapp\snapshot-store\xbm-snapshot.db`(190MB,由 xbm.db backup 生成;`*.db` 已 gitignore) |

数据迁移通道:版本 A 的 `xbm.db` → 版本 B Settings → Import existing .db(merge/replace),
二进制级导入,**向量随行无需重算**(实测 14441 条 5.9 秒)。

---

## 3. 如何启动 / 构建 / 测试

### 版本 A(Python 后端)

```bash
cd C:\MyData\Development\ZCode\xbm
pip install numpy jieba litert-lm "twscrape[curl]" umap-learn hdbscan

# 起服务(模型路径必须可解析:XBM_MODEL_PATH 或 ./models/*.litertlm)
set XBM_MODEL_PATH=C:\Users\fdy10\Downloads\LLM\LiteRTLM\embeddinggemma-2-text-270m.litertlm
python -m xbm serve --db xbm.db --port 8788        # → http://127.0.0.1:8788

# 分离部署(静态前端托管别处连本机后端):
set XBM_CORS_ORIGINS=https://user.github.io        # 跨域白名单(默认仅放行 localhost/*)
python -m xbm serve --host 127.0.0.1               # 0.0.0.1=对局域网开放

# CLI
python -m xbm sync            # 增量同步(twscrape 优先;--full 全量+镜像删除)
python -m xbm embed --backend gpu   # 批量嵌入(CPU ~8.8/s,GPU ~14.7/s)
python -m xbm report / search "查询" / import --archive 存档.zip
```

前端构建(仅改动 `web/` 后需要):`cd web && npm install && npm run build`(产物 `web/dist`,含 109MB wasm)。

单测:`python -m unittest tests.test_all`(27 项;偶发 Windows 临时目录占用导致tearDown
报错,重跑即过——已加 `ignore_cleanup_errors=True`)。

### 版本 B(静态)

```bash
cd C:\MyData\Development\ZCode\xbm\webapp
npm install && npm run build          # 产物 webapp/dist(含 litert wasm + sqlite wasm)

# 本地预览必须带 COOP/COEP 头(server 有,或用 python http.server + 任意静态服务器均可,
# sahpool 不强制要求;index.html 里也内置了 meta 版 COOP/COEP)
python ..\..\tmp_coop_server.py 9999 <dist 绝对路径>
# 或任意能带头/或不带头的服务器——sahpool 实测不依赖头,但建议带头
```

打开 `http://127.0.0.1:9999/` → Settings:
1. **Model**:From URL 填 `http://127.0.0.1:8788/model`(开发期后端下发)或 From file 选
   本地 `.litertlm`;Load 后状态行显示 ready;
2. **Database storage**:Choose/create local .db file(真实文件,可放网盘同步目录)或
   保持 OPFS;Import existing .db(merge/replace)可吃 Python 版 `xbm.db`;
3. **Collect**:拖 Bookmarklet 到书签栏 → Open x.com bookmarks → 在 x.com 页内点书签
   → 数据自动回传入库;
4. Search 直接可用。

版本 B 测试:`webapp` 无自动化单测(待补);浏览器 e2e 流程见 §7。

### 发布仓

```bash
cd C:\MyData\Development\ZCode\xbm-github
git remote add origin https://github.com/<user>/xbm.git && git push -u origin main
# GitHub Release 附件:xbm-v0.1.0-prebuilt.zip(109MB,含 wasm,免 npm 构建)已生成于
# C:\MyData\Development\ZCode\xbm-v0.1.0-prebuilt.zip
```

---

## 4. 两条技术路线详解

### 版本 A(后端中心)

```
x.com ──(twscrape/raw GraphQL,增量遇已知即停)──> xbm serve
                                                    ├─ SQLite(FTS5 trigram 全文 + 向量 BLOB)
                                                    ├─ litert_lm 进程内嵌入(CPU/GPU)
                                                    └─ 分析:类目/双分区聚类/时间线/线索/UMAP
浏览器 UI(同源或 CORS 白名单)──> /api/* ──> 全部重活在后端
```

- 检索:FTS5(中文子串)+ 余弦 → RRF(k=60);作者/日期/媒体过滤;短查询退化 LIKE。
- 分类三维度可交集:类目(零样本)× 关键词聚类(IDF 词向量,字面视角)× LLM 聚类(嵌入,语义视角)。
- 新书签自动:`auto.classify_new` = 补嵌入 + 类目归位 + 双分区簇心归位(≥0.60),**零 LLM**。
- LLM 只做:簇命名(OpenAI 兼容 chat,JSON 约束 few-shot;或 SystemOne 决策接口)、
  时间线叙事(分段)、线索卡片。配置在 Settings 填写或 `~/.xbm/llm.json`。

### 版本 B(浏览器中心,架构对标 LiteRT-LM 官方 embedding_search 示例)

```
静态站(GitHub Pages) ── Bookmarklet 在 x.com 页内采集 ── postMessage 回传
        │  fetch(模型 URL / 用户本地文件)
        ▼
浏览器:WebGPU 嵌入(@litert-lm/core)+ SQLite-WASM(opfs-sahpool OPFS 持久)
检索 = 余弦 + 子串 RRF(JS 移植);LLM = 用户填 URL(本地 llama-server / 远程 API)
```

- 数据主权:OPFS 是浏览器 origin 私有存储(磁盘上真实存在、持久、不上传);
  也可选 File System Access 模式把库放在**用户自选的真实 .db 路径**(可放网盘同步目录,
  免费云同步;句柄存 IndexedDB,重开页面一键重授权)。
- 数据交换:`.db` 二进制导入/导出(serialize/deserialize)+ JSON 导出。

---

## 5. 踩过的坑(按严重程度,全部已修复/有 workaround)

1. **`@litert-lm/core` 必须 ≥0.18**:0.12 没有 `EmbeddingEngine`。npm 版本号形如
   `3.54.1-build1`(sqlite-wasm),`^3.46.0` 会 notarget。
2. **纯文本 270m 模型不能传 visionBackend/audioBackend**:报
   `Model buffer not found for type: 8`。只用 `{model, backend}`。
3. **EmbeddingGemma 前缀必须一致**:查询 `task: search result | query: …`,
   文档 `title: none | text: …`。版本 B 前后端各自硬编码,改要两处同步。
4. **sqlite-wasm bind 只认 Uint8Array 当 BLOB**:Float32Array 会报
   `Unsupported bind() argument type: object`。向量存取用
   `new Uint8Array(f32.buffer, f32.byteOffset, f32.byteLength)`。
5. **opfs-sahpool 不会自动注册**:必须显式 `await sqlite3.installOpfsSAHPoolVfs()`
   再 `new DB(path,'ct','opfs-sahpool')`;否则静默 `no such vfs`(无任何警告,
   耗了数轮排查)。`sqlite3.config.warn` 走 console 不走 printErr——worker 里要先
   劫持 console 才能看到诊断。
6. **WAL 模式镜像 deserialize 报 CANTOPEN**:Python 库是 WAL;导入前把头部字节
   18/19 从 2 改 1(legacy)。webapp 的 `deWal()` 已内置,两条 deserialize 路径都套了。
7. **万级行导入必须单事务**:OPFS 逐行 autocommit = 逐行 fsync,1.4 万行会拖死;
   `BEGIN … COMMIT` 后 5.9 秒完成。
8. **vite build `emptyOutDir` 会清掉 dist 里的测试快照**:durable 快照放
   `webapp/snapshot-store/`,构建后手动拷回 dist。
9. **`sqlite3_js_db_export(db)` 接受 oo1.DB 实例**,返回整库 Uint8Array;
   反序列化用 `sqlite3.capi.sqlite3_deserialize(db.pointer,'main',p,size,size,1|4)`
   (1=FREEONCLOSE,4=RESIZEABLE,内存库必须 RESIZEABLE 否则写满报错)。
10. **Windows 同端口可多个 LISTENING 进程**:重启服务前必须
    `netstat -ano | grep ":8788.*LISTENING"` 全部 taskkill,否则旧行为服务还在接请求。
11. **后台 bash 的工作目录会漂移**:每条命令都显式 `cd` 到项目根;`python -m xbm` 靠
    cwd 找包和相对路径(如 accounts.db 就生成在 cwd)。
12. **GET 路由放错 do_POST**:同一路由在 GET/POST 各有一个 handler 分支,新增端点时
    放错分支会 404(本项目犯过两次:/api/map/points、/api/timeline)。
13. **twscrape 必须 curl 指纹后端**:httpx 的 TLS 指纹被 x.com 403;
    `pip install "twscrape[curl]"` 且进程内设 `TWS_HTTP_BACKEND=curl`(sync.py 已自动)。
14. **X Bookmarks 限速**:500 请求/15 分钟/账号,锁满自动等待续跑;增量同步遇已知
    tweet_id 即停;`--full` 才翻到底并做删除镜像(此次没见到的 = X 端已取消)。
15. **X GraphQL 不返回书签时间戳**:bookmarklet 采集的数据没有 bookmarked_at,
    时间线/线索退回推文发布时间(口径已在 UI 注明)。
16. **思考型小模型(MiniCPM5-2B)命名**:必须 JSON 约束 + few-shot 两例 +
    max_tokens 2048→4096 重试梯;"继续接续前文"类变体提示词会输出空正文;
    50 期长摘要要分 14 行/段。噪声名(句子/超短 ASCII/元词)用白名单+黑名单清洗,
    不过关就诚实"未命名#N"。
17. **凭据别存反**:`x-sync.json` 里 auth_token 字段曾存过字面量导致 403;
    用 `sync --login`/`save_credentials` 写入。
18. **@litert-lm/core 的 wasm 体积 109MB**:不进 git(GitHub 单文件 100MB 限);
    Release 附件分发预构建包。
19. **浏览器嵌入吞吐**:IAB webview 实测 batch16 ≈ 2.2 条/s(32/64 反而更慢),
    Python CPU 8.8/s、GPU 14.7/s。万级首次建库建议走 CLI 导入路径;日常增量浏览器无压力。
20. **File System Access API**:句柄存 IndexedDB 可跨会话,但每次新会话需用户点一次
    授权(requestPermission 不能静默);Firefox/Safari 不支持,自动降级 OPFS。
21. **https 页面连本机服务**:localhost/127.0.0.1 属 secure-context 例外,
    `https://` 静态页可直接 fetch `http://127.0.0.1:8080/v1`(llama-server 自带 CORS),
    所以版本 B 的 LLM URL 方案无需任何代理。
22. **单测偶发 FAILED(Windows 临时文件占用)**:重跑即过,非代码问题。

---

## 6. 版本 A 已实现功能清单(供对接)

- 同步:twscrape/raw GraphQL 双后端、增量/全量、删除镜像、`--login`/`--cookie` 向导、
  官方存档 ZIP 后备导入(旧版 js/新版 json/仅 id 三格式容错)。
- 检索:混合(FTS5 trigram 中文子串 + 余弦 RRF)+ 作者/日期/媒体过滤;CLI + 网页。
- 嵌入:进程内 litert_lm CPU/GPU(设置页可选,带实时进度);批任务 409 防重复。
- 分类:类目零样本(12 类)+ 关键词聚类(1115 簇)+ LLM 聚类(27 簇),三维交集浏览;
  粒度可选(很细/细/适中/粗);仅补命名模式;LLM 双风格命名+测试连接。
- 自动化:sync 后 `auto.classify_new`(嵌入+三维度归位,毫秒级,零 LLM)。
- UMAP 地图(14386 点,37s,PCA 兜底);着色三模式;缩放/平移/点击卡片。
- 时间脉络:按月/季 × 时间窗(全部/24月/12月),LLM 分段叙事(已生成 2673 字全文)。
- 线索账本:中断/持续/新芽/脉冲四区 + sparkline + LLM 卡片(12/12 生成成功)。
- 增量同步/嵌入/聚类/地图/脉络全部**后台任务化**,页面 2-5 秒轮询进度,刷新自动接管。
- 网页删除(单条)与全量同步镜像删除双向。

## 7. 版本 B 已实现功能清单

- OPFS SQLite(sahpool,显式安装)+ 表结构精简版(与 Python 版 tweet 表兼容)。
- 模型:URL / 本地文件 → WebGPU;批量嵌入循环(batch 16)+ 进度。
- Bookmarklet 采集:拖书签栏 → x.com 页内翻页采集(GraphQL 同源,queryId
  `iblrFnKr6PZUR-dWpfXG6g` 硬编码,**会随 x.com 轮换失效,需要自我更新机制**)→
  postMessage 回传(origin 校验 `https://x.com`,测试模式 `?test=1` 收同源)→ 去重合并。
- 「Test import」按钮:10 条合成数据直测合并链路。
- 检索:余弦 + 子串 RRF(JS 版)。
- 存储位置:OPFS / 本地真实 .db 文件(File System Access,句柄持久化+重连+自动保存)
  + 现有 .db 导入(merge/replace,向量随行,已用真实 190MB 库验证)+ JSON 导出。
- 内置嵌入基准(Settings → Benchmark)。

## 8. 版本 B 待办(按优先级)

1. **分析功能 JS 移植**:聚类(词向量引领聚类是纯算法,可直接移植;IDF 稀疏余弦 +
   leader-cluster)、时间脉络(纯 SQL 聚合 + LLM URL)、线索账本(纯计算)。
2. **FTS5/中文子串检索**:sqlite-wasm 构建含 FTS5(需验证 trigram);不行就 JS 端
   trigram 索引兜底。
3. **Bookmarklet queryId 自更新**:正则 x.com 页面 JS bundle 提取新 queryId/features
   (现在硬编码,轮换后 400)。
4. **真实 Chrome 验证**:Bookmarklet 真实采集 + File System Access 选路径 + 真实 Chrome
   嵌入基准(用户尚未做;IAB 无法测文件选择器)。
5. JSON 导入(目前只有 JSON 导出 + .db 导入)。
6. 集成测试自动化(playwright 脚本化 §7 流程)。

## 9. 版本 A 待办

1. 英文 Agent 的双语化收尾(其提交已在 main:`i18n: bilingual UI and CLI (zh/en)`)。
2. 静态版与版本 A 的 schema 拉平(可选:让 CLI 直写 webapp 的 .db 文件,避免 Import 步骤;
   注意双写冲突)。
3. 外链正文抓取 / 图片 OCR(用户明确暂缓)。

---

## 10. 测试记录(怎么验的,结果)

- **单测(版本 A)**:`python -m unittest tests.test_all` → 27 项全过:
  存档三格式解析、去重(首见保留/更早书签时间)、FTS 中文子串、混合检索与过滤、
  raw/twscrape 双后端解析与增量停点、镜像删除、类目分配、双分区聚类独立性、
  线索四类边界、时间脉络聚合、增量归位、投影归一化、线程回调、LLM 名字清洗/systemone。
- **版本 A e2e(浏览器)**:模型 0.9-1.2s WebGPU 加载;语义搜索零字面重叠查询 76-82% 命中;
  14441 条导入后真实查询(REA 逆向)Top1 正确;后台任务(同步/嵌入/聚类/地图/脉络/卡片)
  全部实测进度滚动、断点续跑、刷新接管。
- **版本 B e2e(浏览器,IAB)**:OPFS 建库→Test import 10 条→模型 URL 加载(WebGPU)→
  自动嵌入 10/10→语义搜索 76%→重载持久化→基准 batch16 2.2/s→导入真实 xbm.db
  (190MB,14441 条,5.9s,向量全随行)→重载后 14441 全恢复→真实语料语义搜索 77% 命中。
- **发布仓干净性**:两轮全文件扫描(个人 token/用户名/路径)零命中;`*.db`/cookie 库/日志
  全部 gitignore。

## 11. 关键 API 速查(版本 A)

```
GET  /api/config            模型/前缀配置
GET  /api/status            总量/嵌入/同步任务/嵌入任务/聚类任务/地图任务/脉络任务/卡片任务/twscrape 状态
GET  /api/pending?limit=N   待嵌入文本(前端 WebGPU 用)
POST /api/embeddings        回传向量 [{id, vec}]
POST /api/embed/run         批量嵌入 {backend: cpu|gpu}
POST /api/search            {q, qvec, mode, filters} → 混合检索
GET  /api/categories        类目定义+分布
POST /api/categories/embed  {defs|categories, backend} → 全量分配
GET  /api/clusters          两分区簇列表+粒度/LLM 配置
POST /api/clusters/run      {set: keyword|llm, granularity, recluster, llm_*}
GET  /api/browse?category=&kw=&llm=   三维交集浏览(AND)
POST /api/delete            {tweet_id} 本地删除
GET  /api/map/points?color=category|kw|llm   UMAP 散点
POST /api/map/run           重新投影
GET  /api/timeline?unit=month|quarter&window=all|24|12
POST /api/timeline/run
GET  /api/threads           线索分析(实时计算)
POST /api/threads/cards/run LLM 线索卡片
POST /api/llm/test          LLM 连接测试 {llm_base, llm_model, llm_style}
POST /api/llm/config        保存 LLM 配置
GET  /model                 .litertlm 流式下载(CORS 已开)
POST /api/sync              {backend, full, limit} 后台同步任务
```

版本 B(浏览器内,无 HTTP API):`window.__store.call(op, payload)`(调试用),
op 见 `webapp/src/db-worker.js` HANDLERS;`window.__app` 暴露 engine/switchView。
