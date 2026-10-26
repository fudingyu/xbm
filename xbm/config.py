"""集中配置:路径、Embedding 服务、同步凭据。环境变量可覆盖。"""

import json
import os
from pathlib import Path

# ---- 数据库 ----
def db_path() -> Path:
    return Path(os.environ.get("XBM_DB", Path.cwd() / "xbm.db"))


# ---- Embedding 服务(OpenAI 兼容 /v1/embeddings,由 litert_lm serve 提供) ----
EMBED_BASE = os.environ.get("XBM_EMBED_BASE", "http://127.0.0.1:8081/v1").rstrip("/")
EMBED_MODEL = os.environ.get("XBM_EMBED_MODEL", "embeddinggemma-2-text-270m")

# EmbeddingGemma 系列的官方输入模板(检索质量关键,可用环境变量改)
QUERY_PREFIX = os.environ.get("XBM_QUERY_PREFIX", "task: search result | query: ")
DOC_PREFIX = os.environ.get("XBM_DOC_PREFIX", "title: none | text: ")

EMBED_BATCH = int(os.environ.get("XBM_EMBED_BATCH", "16"))
EMBED_TIMEOUT = int(os.environ.get("XBM_EMBED_TIMEOUT", "60"))

# 批量嵌入主路径:Python 进程内 litert_lm(CPU/GPU)。模型 .litertlm 需自行下载
# (EmbeddingGemma 系列,HuggingFace litert-community 等处有发布),放入 ./models/
# 或用 XBM_MODEL_PATH 指定;默认自动发现 ./models/ 下第一个 .litertlm。
MODEL_PATH = os.environ.get("XBM_MODEL_PATH", "")


def discover_model() -> str:
    """按优先级解析模型路径:环境变量 → ./models/*.litertlm 自动发现 → 空。"""
    if MODEL_PATH:
        return MODEL_PATH
    local = Path("models")
    if local.is_dir():
        for p in sorted(local.glob("*.litertlm")):
            return str(p)
    return ""


MODEL_NOT_FOUND_HINT = (
    "未找到 .litertlm 模型。请下载 EmbeddingGemma 模型文件放入 ./models/ 目录,"
    "或设置环境变量 XBM_MODEL_PATH 指向 .litertlm 文件。")

# ---- 可选:本地 LLM(OpenAI 兼容 /v1/chat/completions),仅用于给聚类簇命名 ----
# 例:llama-server -m qwen3-8b-Q4.gguf 后填 http://127.0.0.1:8080/v1;留空则用关键词自动命名
LLM_BASE = os.environ.get("XBM_LLM_BASE", "")
LLM_MODEL = os.environ.get("XBM_LLM_MODEL", "local")


def llm_config_path() -> Path:
    return Path(os.environ.get("XBM_LLM_CONFIG", Path.home() / ".xbm" / "llm.json"))


def load_llm_config() -> dict:
    """LLM 命名端点配置;持久化文件优先,回退环境变量。style: chat|systemone。"""
    p = llm_config_path()
    if p.is_file():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            return {"base": cfg.get("base") or LLM_BASE,
                    "model": cfg.get("model") or LLM_MODEL,
                    "style": cfg.get("style") or "chat"}
        except Exception:  # noqa: BLE001 - 配置文件损坏时用默认
            pass
    return {"base": LLM_BASE, "model": LLM_MODEL, "style": "chat"}


def save_llm_config(base: str, model: str, style: str = "chat") -> Path:
    p = llm_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"base": base, "model": model, "style": style},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---- GraphQL 增量同步 ----
def sync_config_path() -> Path:
    return Path(os.environ.get("XBM_SYNC_CONFIG", Path.home() / ".xbm" / "x-sync.json"))


def unparsed_dir() -> Path:
    d = Path(os.environ.get("XBM_UNPARSED", Path.home() / ".xbm" / "unparsed"))
    d.mkdir(parents=True, exist_ok=True)
    return d
