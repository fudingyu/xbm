# xbm — X.com 书签本地管理器

[English version](README_README.md) | 中文版

解决 X 书签两大痛点:**无语义/模糊搜索**、**无法按内容分类浏览**。
全部数据与计算留在本机:SQLite + 本地嵌入模型(embeddinggemma, LiteRT-LM,进程内 CPU/GPU)。

```
x.com 书签 ──(增量同步,cookie)──> SQLite(推文 + FTS5 全文索引 + 向量)
                     │                       ↑
                     └── 新书签自动:补嵌入 → 类目/双分区归位(零 LLM)
浏览器 UI(5 视图):搜索 · 分类浏览(三维交集) · UMAP 地图 · 时间脉络 · 线索账本
```

> 仅读取你自己账号的书签(只读);请自行评估并遵守 x.com 服务条款,触发限速时工具会自动等待。

## 准备模型(一次)

下载 EmbeddingGemma 的 `.litertlm` 模型文件(HuggingFace `litert-community` 等处发布,
如 `embeddinggemma-2-text-270m.litertlm`,约 160MB),放入项目下 `models/` 目录:

```bash
mkdir models
# 把下载的 *.litertlm 放进去即可;或用环境变量 XBM_MODEL_PATH 指向任意路径
```

## 快速开始

```bash
pip install -r requirements.txt    # 基础依赖;可选增强见 requirements 内注释

# 1. 同步凭据(一次;cookie 有效期数月)
python -m xbm sync --login        # 向导:自动打开 x.com,引导复制 Cookie,自动解析保存
# 也支持: python -m xbm sync --cookie "auth_token=...; ct0=..."

# 2. 拉书签(twscrape 社区库自动处理接口轮换与限速)
python -m xbm sync                # 增量:遇到已知推文即停;首次可 --full 分批多次
# 未装 twscrape 时自动退回内置 raw GraphQL(需 --init 后手填 endpoint)

# 3. 批量嵌入 + 自动分类
python -m xbm embed --backend gpu # CPU ~9条/s,GPU(WebGPU)~15条/s;也可在网页点按钮

# 4. 起 Web UI(仓库已含构建好的 web/dist;改动 web/ 源码后才需重新构建)
python -m xbm serve               # http://127.0.0.1:8788
```

页面五个视图:混合检索、三维交集分类浏览、UMAP 地图、时间脉络、线索账本(中断线索回溯)。

前端构建(仅改动 web/ 源码后需要):

```bash
cd web && npm install && npm run build   # 产物 web/dist,含 wasm 运行时
```

## CLI 一览

| 命令 | 说明 |
|---|---|
| `sync --login / --cookie ...` | 导入凭据(存 `~/.xbm/x-sync.json`,仅本机) |
| `sync [--full] [--limit N]` | 增量/全量同步书签(twscrape 优先,raw 备选) |
| `search "查询" [--mode auto\|fts\|vector] [--author A] [--after 2025-01-01]` | 命令行混合检索 |
| `report` | 数据质量报告(媒体/外链/语言/长度分布,决定增强项取舍) |
| `import --archive 存档.zip` | 后备:官方存档 ZIP/目录导入(旧版 js/新版 json/仅 id 三种格式均容错) |
| `serve [--port 8788]` | 本地 Web 服务(静态前端 + API + 模型流) |
| `embed` | 可选:服务端嵌入(OpenAI 兼容 /v1/embeddings,如 litert_lm serve);主路径是浏览器 WebGPU |

## 分类浏览(M3:三维度 + 交集筛选)

「分类浏览」视图提供**三个独立分类维度**,可任意组合交集筛选(点选高亮、再点取消):

| 维度 | 聚类方法(两法正交,交集才有叠加增益) | 当前规模 |
|---|---|---|
| 类目(零样本语义) | 「刷新类目分配」,服务端嵌入类目描述 + 余弦 softmax | 12 类目 × 全量 |
| 关键词聚类 | **IDF 加权词向量引领聚类**(字面词汇视角:jieba 分词 → IDF 稀疏向量 → 余弦引领聚类,阈值 0.09-0.14) | 1115 簇(展示前 60) |
| LLM 聚类 | **嵌入语义引领聚类**(语义向量视角,阈值 0.62-0.70)+ LLM 命名 | 27 簇 |

两套聚类方法完全不同(字面 vs 语义),同一主题在两边的簇不会重合,交集 = 「语义相近
**且** 用词相同」→ 精准收窄(实测:语义簇 862 条 ∩ 字面簇 9 条 = 9 条纯 llama-server 调参内容)。
簇心一律用嵌入质心,增量归位与地图着色两法统一。

- **两套聚类完全独立**(各存各的簇结构、粒度可不同),互不覆盖;LLM 无产出的簇诚实显示"未命名#N",
  不会拿关键词名冒充——之后可取消勾选「重新聚类」**仅补命名**(簇结构不变)。
- 聚类为后台任务,页面 2 秒轮询实时进度(命名阶段显示"第 x/y 簇"+当前簇预览),刷新页面自动接管。
- LLM 命名两种接口风格:**OpenAI 兼容**(自由文本;对小模型输出做字符白名单 + 元词黑名单清洗,
  不过关一律留空)与 **SystemOne 决策**(`/v1/systemone`,需 decision 模型,普通模型 501 自动回退);
  「测试连接」按钮秒级验证配置。
- 嵌入后端(CPU ~9 条/s / GPU ~15 条/s)在搜索页下拉选择;模型输入上限 1024 token,中文截断 850 字符。
- 嵌入主路径为 Python 进程内 litert_lm,后端可选:**CPU(XNNPACK)约 8.8 条/秒,
  GPU(WebGPU/780M)约 14.7 条/秒(+67%)**,网页「嵌入后端」下拉框选择并点「批量嵌入」
  (带实时进度),或 CLI `python -m xbm embed --backend cpu|gpu`;浏览器 WebGPU 保留为查询向量兜底。
  模型输入上限 1024 token,中文文本按 850 字符截断。
- 经验:Gemma 系嵌入基线余弦高(无关文本 ~0.5-0.6),聚类阈值需在 0.6+ 区间;
  HDBSCAN 在同质语料上只会出巨簇,故大数据集用抽样引领聚类。

## 线索(兴趣账本,双轨视角)

第五个标签页「线索」:**把推荐引擎的动态与自己的知识资产分开**。

月度统计容易沦为算法的镜子(热点周期性起伏);本页按每个主题簇的时间活动剖面
做确定性分类(同步后自动更新,无需 LLM):

| 类别 | 判定 | 用途 |
|---|---|---|
| ⚠ **中断线索** | 曾活跃 ≥2 月、已沉寂 ≥4 月 | **核心交付**:"曾经关注、被断供后遗忘"的方向,定期回看与重启 |
| ✓ 持续线索 | 活跃 ≥4 月或跨度 ≥半年 | 系统性兴趣(知识资产),附近 3 月动能 |
| 🌱 新芽 | 首次出现距今 <2 月 | 观察是否值得投入 |
| ⚡ 热点脉冲 | 活跃 ≤2 月即消失 | 算法热点,浏览过即可,不占心智 |

- 每条线索:名称 + 近 12 月活跃火花线 + 规模/跨度/沉寂统计,点击直达该书签列表
  (可与类目/另一分区做交集筛选);
- 「生成线索解读(LLM)」:为中断 Top6 + 持续 Top6 写卡片(是什么 / 下一步深入建议,
  基于已收集内容指出缺口),结果缓存;纯统计功能永远不需要 LLM。

## 时间脉络(关注焦点演变)

顶部第四个标签页「时间脉络」:按月/季聚合全部书签的主题统计(类目 + LLM 语义簇 +
关键词字面簇 + 代表推文),LLM 据此撰写**关注焦点随时间演变的叙事**(逐期一两句 +
整体主线总结),结果缓存本地(meta 表),可反复查看、随时重新生成。

- 时间窗:全部 / 近 24 月 / 近 12 月;时间口径优先书签时间,缺失时用推文发布时间
  (早年收藏的经典老帖会出现在历史期);
- 长时间范围自动**分段生成**(每段独立走重试梯子,单段失败跳过不报废整体);
  思考型小模型对"继续接续前文"类变体提示词会输出空正文,故每段都用与整版相同的模板;
- 未配置 LLM 时仅生成统计摘要;LLM 失败时统计照常可用并在状态行注明。

## 增量分类(新内容自动归位,零 LLM)

新书签进来后**不需要重扫、不需要 LLM**:

```
sync → 补嵌入(CPU/GPU 批量)→ 类目:对持久化类目向量余弦(N 个类目)
                            → 双分区:对簇心余弦 ≥0.60 归位(毫秒级)
```

- 簇分区即"自动标签体系"(LLM 只在簇诞生时命名一次);类目向量存 meta、簇心存
  cluster 表(老分区缺簇心时自动回填,不重聚类不动名字);
- CLI `sync` 与网页「增量同步」按钮都会自动执行归位并在结果里报数;
  离群书签(簇心余弦 <0.60)留空,攒多了手动「重新聚类」吸收;
- LLM 的唯一职责:新簇诞生时命名(几十次调用)、可选改名——绝不全量扫描。

## 设计要点

- **数据获取**:twscrape(`api.bookmarks()`,2.9k★ 活跃维护)为首选后端(自动切 curl_cffi
  指纹伪装,规避 x.com 对 httpx 的 403);内置 raw GraphQL 为零依赖备选;官方存档 ZIP 仅作离线后备。
  增量逻辑:书签按时间倒序,遇到已知 tweet_id 即停,可 `--limit`/`--max-pages` 分批吃完。
  **删除镜像**:`sync --full` 翻到书签末尾后,库里"这次没见到"的 graphql 书签视为已在 X 端取消,自动删除;
  网页里也可对单条结果点"删除"(仅本地)。
- **文本保真**:入库保留推文原始换行(展示层"查看更多"展开/收起);嵌入向量另用折叠文本计算;
  原始 rawContent 存 raw_json 列,便于未来重清洗。
- **嵌入**:主路径为 Python 进程内 litert_lm(CPU/GPU 可选);模型路径按
  `XBM_MODEL_PATH` → `./models/*.litertlm` 自动发现的顺序解析。
  EmbeddingGemma-2 输入模板:查询 `task: search result | query: …`,文档 `title: none | text: …`。
- **检索**:FTS5 trigram(中文子串,如"旅行攻略")+ 余弦语义 + RRF(k=60)融合;
  支持作者/日期/媒体过滤;短查询(<3 字符)自动退化为 LIKE。
- **去重**:按 tweet_id 首见保留(后源不覆盖);书签时间取最早;GraphQL 无书签时间时以 first_seen 近似。

## 已知限制与注意

- 只读自己账号的书签,遵守 x.com ToS 风险自担;触发 429 时等待几分钟再跑。
- 书签里的**图片内容**暂未参与语义检索(文本盲区,后续可加 OCR/多模态)。
- 外链文章正文未抓取(很多书签本质是"稍后读",后续可加)。
- WebGPU 需较新的 Chrome/Edge;页面会给出明确错误提示。

## 部署形态

**默认(同源)**:一条 `python -m xbm serve` 起全部——静态前端、API、模型流都在本机 8788。

**分离部署**(静态前端托管到 GitHub Pages / 任意静态服务器,架构同 LiteRT-LM 官方
embedding_search 示例):前端是纯静态站点;每个用户在本机跑后端,数据与模型永远留在本地。

```
静态前端(GitHub Pages,如 https://user.github.io/xbm-web/)
        │ fetch('http://127.0.0.1:8788/api/…')  ← CORS 跨域
        ▼
xbm serve(用户本机:SQLite 数据库 + .litertlm 模型)
```

| 环境变量 | 作用 |
|---|---|
| `XBM_CORS_ORIGINS` | 跨域白名单(逗号分隔,如 `https://user.github.io`)。**不设则仅放行 localhost/127.0.0.1 任意端口**;陌生来源拿不到 CORS 头,本地数据不会被任意网页读取 |
| `XBM_HOST` | 监听地址,默认 `127.0.0.1`;设 `0.0.0.0` 对局域网开放(前端跑在另一台机器时) |
| `XBM_MODEL_PATH` | 模型路径(默认自动发现 `./models/*.litertlm`) |

前端侧约定:分离部署时 API 基址指向 `http://127.0.0.1:8788`(构建期 `VITE_API_BASE`
或页面内配置);`/model` 由后端流式下发(约 160MB,首次加载后浏览器缓存)。

### 静态版(webapp/,纯前端 —— 实验性 WIP,数据层仍在开发中)

`webapp/` 是独立于后端的**纯静态版本**:数据存浏览器 OPFS(SQLite-WASM),模型在
浏览器内加载(WebGPU),内置书签收集器(bookmarklet)——浏览 x.com 时一键收藏。
构建:`cd webapp && npm install && npm run build`(产物 `webapp/dist`,可托管到
GitHub Pages 等任意静态服务器)。适合免安装体验与随手收集;完整功能(聚类/时间
脉络/线索等)仍在服务版 `web/`。

## 测试

```bash
python -m unittest tests.test_all -v   # 12 项:存档三格式/去重/FTS/混合检索/双后端解析
```

## 项目结构

```
xbm/            Python 包(零第三方依赖,除 numpy;twscrape 可选)
  sync.py       twscrape + raw GraphQL 双后端、--login 向导
  importer.py   多源统一入库与去重
  search.py     FTS+向量+RRF 混合检索
  server.py     本地 API/静态/模型流服务
  embed.py      向量存取(BLOB float32)
web/            浏览器前端(vite + @litert-lm/core,WebGPU 嵌入)
tests/          单测 + 合成存档 fixture 生成器
```
