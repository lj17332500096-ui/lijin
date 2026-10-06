"""本地文档问答（RAG）：关键词（BM25）+ 向量（ONNX 本地模型）混合检索。

支持的文件：
- 常规文本：.md/.txt/.py/.tsx/.json/.csv 等；
- PDF（文本层）：装了 pypdf 时自动抽取每页文字入库，检索结果带页码；
  扫描版 PDF（无文本层）会跳过并在建索引时提示，可用 ask_image 逐页截图提问。

检索策略：
- 纯本地、零远程接口：向量用 onnxruntime 跑本地 embedding 模型（如 BGE 中文系），
  模型目录通过 RAG_EMBED_MODEL 指定（或放在项目 models/ 下自动发现）；
- 向量就绪时：BM25 与 向量余弦分别取 top-N，再按 RRF（倒数排名融合）合并；
  同义改写（“锻炼”→“健身”）也能命中；
- 向量不可用（未配置/目录缺失/依赖缺失）时：自动降级为纯 BM25 关键词检索，
  并在检索结果里附一行说明，功能不受影响。
"""

import json
import math
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

from agents import function_tool
from dotenv import load_dotenv
from runtime.tool_errors import tool_failure_error_function
from runtime_paths import generated_path

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or BASE_DIR.parent).resolve()
INDEX_PATH = generated_path("cache", "rag_index.json")
# P1-3：索引写盘互斥锁（并发 index_workspace / 自动建索引串行化）
_INDEX_WRITE_LOCK = threading.Lock()

TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml",
    ".csv", ".html", ".css", ".toml", ".ini", ".cfg",
    ".pdf",
    ".docx", ".xlsx", ".pptx",
}
SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".idea", "traces", ".codex", "data", "var", "models", "code_sandbox"}
SKIP_FILES = {".env", "apikey.txt", "memory.json", "sessions.sqlite", "rag_index.json"}
MAX_FILE_BYTES = 2 * 1024 * 1024
# 无扩展名的常见文本文件（GitHub 仓库里常有 README/LICENSE 不带后缀）
NAMED_TEXT_FILES = {"README", "LICENSE", "CHANGELOG", "AUTHORS", "CONTRIBUTING", "COPYING", "NOTICE", "SECURITY"}
CHUNK_SIZE = 900
CHUNK_OVERLAP = 150
DEFAULT_AUTO_FILES = 800
INDEX_VERSION = 2

# 向量检索配置
EMBED_MODEL_SETTING = os.getenv("RAG_EMBED_MODEL", "").strip()
EMBED_DEFAULT_DIR = BASE_DIR / "models" / "bge-small-zh-v1.5"
EMBED_MAX_CHUNKS = 6000        # 超过该分块数不建向量（降级 BM25）
EMBED_BATCH = 32
RRF_TOP = 200                  # 每路召回候选数
RRF_K = 60                     # RRF 平滑常数

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokens(text: str) -> list[str]:
    """分词：英文/数字按词，中文按相邻二元组（无需分词库）。"""
    low = text.lower()
    tokens = _WORD_RE.findall(low)
    cjk = _CJK_RE.findall(low)
    tokens.extend("".join(pair) for pair in zip(cjk, cjk[1:]))
    return [t for t in tokens if len(t) >= 2]


def _chunk_text(text: str) -> list[str]:
    """按 ~900 字符切块，尽量在换行处断开，保留 150 字符重叠。"""
    if len(text) <= CHUNK_SIZE:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            nl = text.rfind("\n", start + CHUNK_OVERLAP, end)
            if nl != -1:
                end = nl + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _iter_text_files(root: Path, max_files: int):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if count >= max_files:
                return
            path = Path(dirpath) / name
            if name in SKIP_FILES or name.startswith(".env"):
                continue
            if path.suffix.lower() not in TEXT_EXTS and name not in NAMED_TEXT_FILES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path
            count += 1


# ---------------------------------------------------------------------------
# PDF 文本层抽取（pypdf 为可选依赖；缺失时 PDF 自动跳过）
# ---------------------------------------------------------------------------

_PDF_MIN_PAGE_CHARS = 20
_pdf_checked = False
_pdf_available = False


def _pdf_support() -> bool:
    global _pdf_checked, _pdf_available
    if not _pdf_checked:
        try:
            import pypdf  # noqa: F401
            _pdf_available = True
        except ImportError:
            _pdf_available = False
        _pdf_checked = True
    return _pdf_available


def _extract_pdf_pages(path: Path) -> list[str]:
    """抽取 PDF 每页文本；无文本层的页返回空字符串。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        pages.append(text)
    return pages


# ---------------------------------------------------------------------------
# 本地 ONNX 向量模型加载（不依赖 fastembed / 网络；缺依赖时向量功能自动关闭）
# ---------------------------------------------------------------------------

class EmbeddingEngineError(RuntimeError):
    pass


def _embed_enabled() -> bool:
    """向量开关：RAG_EMBED_MODEL=off/空 且默认目录不存在 → 关闭。"""
    setting = EMBED_MODEL_SETTING.lower()
    if setting in ("off", "none", "0", "false", "disabled"):
        return False
    if EMBED_MODEL_SETTING:  # 显式指定了路径（目录或 fastembed 模型名）
        return True
    return EMBED_DEFAULT_DIR.is_dir()


def _resolve_embed_model_dir() -> Path | None:
    """解析模型目录：显式路径优先，其次项目内默认目录。"""
    if EMBED_MODEL_SETTING and EMBED_MODEL_SETTING.lower() not in ("off", "none", "0", "false", "disabled"):
        p = Path(EMBED_MODEL_SETTING).expanduser()
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        return p if p.is_dir() else None
    return EMBED_DEFAULT_DIR if EMBED_DEFAULT_DIR.is_dir() else None


_EMBEDDER: object | None = None
_EMBED_ERROR: str | None = None
_EMBEDDER_LOADED = False


def _try_load_embedder() -> tuple[object | None, str | None]:
    r"""加载一次向量引擎；返回 (engine, 错误说明)。加载失败会缓存原因，避免反复尝试。

    ⚠️ **重载路径必须先清空 `_EMBEDDER`/`_EMBED_ERROR`**（2026-10-05 加固）。
    旧写法先置 `_EMBEDDER_LOADED = True`、**再**判 `_embed_enabled()`，而
    `enabled=False` 的早退分支**没有清 `_EMBEDDER`** ⇒ 模块级状态会变成
    「标志位=True + 陈旧引擎对象」的自相矛盾态；紧接着的第二次调用命中
    `_EMBEDDER_LOADED` 缓存短路，会把**上一次那个陈旧引擎**原样交出去。

    📌 **归因（10-06 交叉实验实测，勿再改回，也不要把下面两段对调）**：

    - 该不自洽态**在生产路径上不可达**。全仓唯一把 `_EMBEDDER_LOADED` 置回
      False 的地方是模块初始化（`grep -rn "_EMBEDDER_LOADED\s*=\s*False"
      --include=*.py .` 仅命中本文件），生产代码没有任何重置点，
      而陈旧态的必要条件正是「`_EMBEDDER` 有值 + `_EMBEDDER_LOADED=False`」。
    - 真正触发它的是**测试隔离缺陷**：`tests/test_rag.py` 的 setUp/tearDown
      曾只重置 `_EMBEDDER_LOADED`、把 `_EMBEDDER` 留在原地。
    - 交叉实验（10-06，见 `var/04-test/测试报告.md` §A4）证明**两侧修法各自
      独立**都能让测试全绿：只修测试侧 ⇒ 22 passed；只修本产品侧 ⇒ 同样全绿。

    ⇒ 本处改动是**第二道防线**，不是「测试走 hybrid 的根」。之所以仍要保留：
    「只重置标志位」的调用方一旦再次出现（例如新测试类、新的重试入口），
    错误应在**产生处**收敛，而不是指望每个调用方都记得清理对象。
    """
    global _EMBEDDER, _EMBED_ERROR, _EMBEDDER_LOADED
    if _EMBEDDER_LOADED:
        return _EMBEDDER, _EMBED_ERROR
    # 重载：先清空旧值，保证「置 True」之后三个全局彼此自洽
    _EMBEDDER = None
    _EMBED_ERROR = None
    _EMBEDDER_LOADED = True

    if not _embed_enabled():
        return None, None

    model_dir = _resolve_embed_model_dir()
    if model_dir is None:
        _EMBED_ERROR = (
            f"未找到向量模型目录（按 {EMBED_MODEL_SETTING or EMBED_DEFAULT_DIR} 查找），"
            f"已自动降级为关键词检索；把模型放到该目录即可启用语义检索"
        )
        return None, _EMBED_ERROR

    engine = None
    error = None
    try:
        engine = OnnxEmbedEngine(model_dir)
        engine.load()
        _EMBEDDER = engine
    except EmbeddingEngineError as exc:
        error = str(exc)
        engine = None      # 半成品（已构造、未 load）绝不能交出去
    except Exception as exc:  # 依赖缺失、文件损坏等一律降级，不中断主流程
        error = f"向量模型加载失败（{type(exc).__name__}: {exc}）"
        engine = None      # 同上：否则调用方 `if engine is None` 会误判为加载成功
    _EMBED_ERROR = error
    return engine, error


class OnnxEmbedEngine:
    """极简 ONNX embedding 推理：CLS 池化 + L2 归一化（与 fastembed 同构）。"""

    ONNX_CANDIDATES = ("model_optimized.onnx", "model.onnx", "onnx/model.onnx")

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self._session: object | None = None
        self._tokenizer: object | None = None
        self._max_len = 512
        self._pad_id = 0
        self._dim = 0
        self._np = None

    def _find_onnx(self) -> Path:
        for name in self.ONNX_CANDIDATES:
            candidate = self.model_dir / name
            if candidate.is_file():
                return candidate
        found = sorted(self.model_dir.glob("*.onnx"))
        if found:
            return found[0]
        raise EmbeddingEngineError(f"模型目录 {self.model_dir} 里找不到 .onnx 模型文件")

    def _require(self, name: str) -> Path:
        path = self.model_dir / name
        if not path.is_file():
            raise EmbeddingEngineError(f"模型目录 {self.model_dir} 缺少必需文件 {name}")
        return path

    def load(self) -> None:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        try:
            config = json.loads(self._require("config.json").read_text(encoding="utf-8"))
        except OSError:
            raise EmbeddingEngineError("config.json 读取失败")
        try:
            tok_cfg = json.loads(self._require("tokenizer_config.json").read_text(encoding="utf-8"))
        except OSError:
            raise EmbeddingEngineError("tokenizer_config.json 读取失败")

        max_len = tok_cfg.get("model_max_length") or tok_cfg.get("max_length")
        if not isinstance(max_len, int) or max_len <= 0:
            raise EmbeddingEngineError("tokenizer_config.json 里缺少有效的 model_max_length")
        self._max_len = max_len
        self._pad_id = int(config.get("pad_token_id", 0))

        self._tokenizer = Tokenizer.from_file(str(self._require("tokenizer.json")))
        self._tokenizer.enable_truncation(max_length=self._max_len)
        self._tokenizer.enable_padding(
            pad_id=self._pad_id, pad_token=str(tok_cfg.get("pad_token", "<pad>") or "<pad>")
        )

        self._session = ort.InferenceSession(
            str(self._find_onnx()), providers=["CPUExecutionProvider"]
        )
        # 确认输入张量名称（兼容 input_ids+attention_mask，或仅 input_ids）
        in_names = [inp.name for inp in self._session.get_inputs()]
        if not any("input" in n.lower() for n in in_names):
            raise EmbeddingEngineError(f"模型输入名无法识别：{in_names}")
        self._dim = self._session.get_outputs()[0].shape[-1]
        if not isinstance(self._dim, int):
            self._dim = 0
        self._np = np

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """对一批文本做向量化（内部自动分批），返回归一化后的向量列表。"""
        if self._session is None or self._tokenizer is None:
            raise EmbeddingEngineError("向量模型尚未加载")
        np = self._np
        results: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            encodings = self._tokenizer.encode_batch(batch)
            input_ids = np.stack([e.ids for e in encodings]).astype(np.int64)
            attention = np.stack([e.attention_mask for e in encodings]).astype(np.int64)
            feeds: dict = {"input_ids": input_ids}
            for name in ("attention_mask", "token_type_ids", "input_type_ids"):
                if name in {i.name for i in self._session.get_inputs()}:
                    feeds[name] = np.zeros_like(input_ids) if "type" in name else attention
            outputs = self._session.run(None, feeds)[0]
            if outputs.ndim == 3:  # (batch, seq, dim) → 取首 token（CLS）
                outputs = outputs[:, 0]
            outputs = outputs / np.linalg.norm(outputs, axis=1, keepdims=True)
            for row in outputs.astype(np.float32):
                results.append([round(float(x), 6) for x in row])
        return results

    @property
    def dim(self) -> int:
        return self._dim


def _embed_query(engine: object, query: str) -> list[float] | None:
    try:
        vectors = engine.embed_batch([query[:2000]])
        return vectors[0] if vectors else None
    except Exception:
        return None


def _rrf_merge(bm25_ranked: list[int], dense_ranked: list[int], top_k: int) -> list[int]:
    """倒数排名融合（RRF）：两条排名列表合并成一条。"""
    scores: Counter = Counter()
    for rank, chunk_id in enumerate(bm25_ranked[:RRF_TOP]):
        scores[chunk_id] += 1.0 / (RRF_K + rank + 1)
    for rank, chunk_id in enumerate(dense_ranked[:RRF_TOP]):
        scores[chunk_id] += 1.0 / (RRF_K + rank + 1)
    return [chunk_id for chunk_id, _ in scores.most_common(top_k)]


def _cosine_scores(query_vec: list[float], vectors: list[list[float]]) -> list[float]:
    """纯 Python 余弦相似度（向量已归一化，点积即相似度）。"""
    scores = []
    for vec in vectors:
        acc = 0.0
        for a, b in zip(query_vec, vec):
            acc += a * b
        scores.append(acc)
    return scores


def _top_indices(scores: list[float], top_k: int) -> list[int]:
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return order[:top_k]


# ---------------------------------------------------------------------------
# 索引与检索
# ---------------------------------------------------------------------------

def _index_vectors_usable(saved_model: object) -> bool:
    """磁盘索引里的向量**还能不能用**（`RagIndex.load` 的闸门）。

    ⚠️ 这是一条**独立于 `_EMBEDDER_LOADED` 的污染通道**（2026-10-06 实测，
    `var/_probe_load.py`）：`load()` 曾只校验 `len(vectors) == len(chunks)`
    就把向量恢复进内存，**完全不检查 `embed_model`**—— 而 `save()` 明明把它
    写进了 payload。⇒ 模型 X 建的索引被模型 Y（或关向量的配置）读出来时，
    陈旧向量会一直躺在 `self.vectors` 里。

    为什么**当时没立刻误召回**：`_dense_rank` 里还有
    `engine, _ = _try_load_embedder(); if engine is None: return []`，
    配置为 off 时引擎必为 None ⇒ 走不到向量路径。**但那是检索期的第二道防线，
    不是加载期的**。一旦换配置/换机器使引擎可用，这些陈旧向量就会参与排序，
    且与当前分块对不上号。

    这里在**加载期**就把它挡住，三条判据：
      ① 向量开关已关闭 ⇒ 一律不用（声明关了就是关了，不能被磁盘内容推翻）；
      ② 索引没记 `embed_model`（旧版本索引）⇒ 保守丢弃，重新建；
      ③ 记录的模型与当前不一致 ⇒ 丢弃，交给 `_ensure_vectors` 重建。
    丢弃后 `self.vectors` 保持 `None`，`_ensure_vectors` 会按当前配置重算。
    """
    if not _embed_enabled():
        return False
    if not isinstance(saved_model, str) or not saved_model:
        return False
    return saved_model == str(EMBED_MODEL_SETTING or EMBED_DEFAULT_DIR)


class RagIndex:
    """本地检索索引：文件清单 + 文本分块；可选携带向量（dense）做混合检索。"""

    def __init__(self, root: Path):
        self.root = root
        self.version = INDEX_VERSION
        self.files: list[dict] = []
        self.chunks: list[dict] = []
        self.vectors: list[list[float]] | None = None  # 与 chunks 对齐；None=未建向量
        self.pdf_support: bool = False
        self._token_cache: dict[int, Counter] = {}
        self._inverted: dict[str, set[int]] = {}
        self._avg_len = 1.0
        self._notes: list[str] = []

    # ---- 构建 ----

    def build(self, max_files: int = DEFAULT_AUTO_FILES, with_vectors: bool = True) -> tuple[int, int]:
        """扫描并分块，返回 (文件数, 分块数)；分块不多时可顺带计算向量。"""
        files = []
        chunks: list[dict] = []
        pdf_unsupported: list[str] = []   # 装了 pypdf 才能抽的 PDF
        pdf_scanned: list[str] = []       # 无文本层（扫描版）的 PDF
        for path in _iter_text_files(self.root, max_files):
            rel = path.relative_to(self.root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                continue
            suffix = path.suffix.lower()
            files.append({"path": rel, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})

            if suffix == ".pdf":
                if not _pdf_support():
                    pdf_unsupported.append(rel)
                    continue
                try:
                    pages = _extract_pdf_pages(path)
                except Exception:
                    continue  # 损坏/加密的 PDF 跳过
                page_texts = [(i + 1, text) for i, text in enumerate(pages) if len(text) >= _PDF_MIN_PAGE_CHARS]
                if not page_texts:
                    pdf_scanned.append(rel)
                    continue
                for page_no, page_text in page_texts:
                    offset = 0
                    for piece in _chunk_text(page_text):
                        line_no = page_text.count("\n", 0, offset) + 1
                        chunks.append(
                            {
                                "id": f"{rel}#{len(chunks)}",
                                "file": rel,
                                "page": page_no,
                                "line": line_no,
                                "text": piece,
                            }
                        )
                        offset += max(len(piece), 1)
                continue

            if suffix in (".docx", ".xlsx", ".pptx"):
                try:
                    from integrations.office_docs import extract_sections

                    sections = extract_sections(path)
                except Exception:
                    continue  # 解析失败/缺库的 Office 文件跳过
                for label, section_text in sections:
                    if not section_text.strip():
                        continue
                    offset = 0
                    for piece in _chunk_text(section_text):
                        line_no = section_text.count("\n", 0, offset) + 1
                        chunk: dict = {
                            "id": f"{rel}#{len(chunks)}",
                            "file": rel,
                            "line": line_no,
                            "text": piece,
                        }
                        if label:
                            chunk["part"] = label
                        chunks.append(chunk)
                        offset += max(len(piece), 1)
                continue

            try:
                data = path.read_bytes()
            except OSError:
                continue
            if _is_binary(data):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            offset = 0
            for piece in _chunk_text(text):
                line_no = text.count("\n", 0, offset) + 1
                chunk_id = f"{rel}#{len(chunks)}"
                chunks.append(
                    {
                        "id": chunk_id,
                        "file": rel,
                        "line": line_no,
                        "text": piece,
                    }
                )
                offset += max(len(piece), 1)
        self.files = files
        self.chunks = chunks
        self.pdf_support = _pdf_support()
        if pdf_unsupported:
            self._notes.append(
                f"有 {len(pdf_unsupported)} 个 PDF 未索引（缺 pypdf，pip install pypdf 后重建索引即可）"
            )
        if pdf_scanned:
            names = "、".join(pdf_scanned[:3]) + ("…" if len(pdf_scanned) > 3 else "")
            self._notes.append(
                f"有 {len(pdf_scanned)} 个扫描版 PDF 没有文本层已跳过（{names}），"
                f"可把它们某一页导出成图片后用 ask_image 提问"
            )
        if with_vectors and len(chunks) <= EMBED_MAX_CHUNKS:
            self._build_vectors()
        elif with_vectors:
            self._notes.append(
                f"分块 {len(chunks)} 超过向量上限 {EMBED_MAX_CHUNKS}，本次仅建关键词索引"
            )
        return len(files), len(chunks)

    def _build_vectors(self) -> None:
        engine, error = _try_load_embedder()
        if engine is None:
            if error:
                self._notes.append(error)
            self.vectors = None
            return
        texts = [chunk["text"][:2000] for chunk in self.chunks]
        vectors = engine.embed_batch(texts)
        if len(vectors) == len(self.chunks):
            self.vectors = vectors
        else:
            self.vectors = None
            self._notes.append("向量计算数量不匹配，本次仅建关键词索引")

    # ---- 存取 ----

    def save(self) -> None:
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {
            "version": self.version,
            "root": str(self.root),
            "built_at": time.time(),
            "pdf_support": self.pdf_support,
            "files": self.files,
            "chunks": self.chunks,
        }
        if self.vectors is not None:
            payload["embed_model"] = str(EMBED_MODEL_SETTING or EMBED_DEFAULT_DIR)
            payload["vectors"] = self.vectors
        # P1-3：原子写——写临时文件再 rename，避免并发 load() 读到半截 JSON
        # （index_workspace 重建与 search_documents 读索引可能交错）。
        tmp_path = INDEX_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(INDEX_PATH)

    @classmethod
    def load(cls, root: Path) -> "RagIndex | None":
        if not INDEX_PATH.exists():
            return None
        try:
            payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        index = cls(root)
        index.version = payload.get("version", 1)
        index.files = payload.get("files", [])
        index.chunks = payload.get("chunks", [])
        index.pdf_support = bool(payload.get("pdf_support", False))
        if isinstance(payload.get("vectors"), list):
            vectors = payload["vectors"]
            if len(vectors) == len(index.chunks) and _index_vectors_usable(
                    payload.get("embed_model")):
                index.vectors = vectors
        return index

    # ---- 懒准备 ----

    def _prepare(self) -> None:
        """构建倒排索引与平均长度（检索前懒执行一次）。"""
        if self._inverted:
            return
        lengths = []
        for i, chunk in enumerate(self.chunks):
            counter = Counter(_tokens(chunk["text"]))
            self._token_cache[i] = counter
            lengths.append(sum(counter.values()))
            for token in counter:
                self._inverted.setdefault(token, set()).add(i)
        total = sum(lengths) or 1
        self._avg_len = total / max(len(lengths), 1)

    def _ensure_vectors(self) -> None:
        """老索引升级：分块已有但没有向量时，现场补算一次并落盘。"""
        if self.vectors is not None or not self.chunks:
            return
        if len(self.chunks) > EMBED_MAX_CHUNKS:
            self._notes.append("分块数量超过向量上限，保持关键词检索")
            return
        engine, error = _try_load_embedder()
        if engine is None:
            if error:
                self._notes.append(error)
            return
        vectors = engine.embed_batch([c["text"][:2000] for c in self.chunks])
        if len(vectors) == len(self.chunks):
            self.vectors = vectors
            try:
                self.save()
            except OSError:
                pass
        else:
            self._notes.append("向量补算数量不匹配，保持关键词检索")

    def is_stale(self) -> bool:
        """索引里的文件是否被改动/删除；或 PDF 支持状态变化需要重建。"""
        if not self.files:
            return True
        if self._pdf_flag_changed():
            return True
        for meta in self.files:
            path = self.root / meta["path"]
            try:
                stat = path.stat()
            except OSError:
                return True
            if stat.st_mtime_ns != meta["mtime_ns"] or stat.st_size != meta["size"]:
                return True
        return False

    def _pdf_flag_changed(self) -> bool:
        """装了/卸载 pypdf 后，带 PDF 的索引应重建。"""
        if not any(f["path"].lower().endswith(".pdf") for f in self.files):
            return False
        return self.pdf_support != _pdf_support()

    # ---- 检索 ----

    def _bm25_rank(self, query: str, top_k: int = RRF_TOP) -> list[int]:
        self._prepare()
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []
        n_docs = max(len(self.chunks), 1)
        scores: Counter = Counter()
        k1, b = 1.5, 0.75
        for token in query_tokens:
            postings = self._inverted.get(token, set())
            df = len(postings)
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
            for chunk_id in postings:
                tf = self._token_cache[chunk_id].get(token, 0)
                length = sum(self._token_cache[chunk_id].values())
                norm = length / self._avg_len if self._avg_len else 1.0
                scores[chunk_id] += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * norm))
        return [i for i, _ in scores.most_common(top_k)]

    def _dense_rank(self, query: str, top_k: int = RRF_TOP) -> list[int]:
        self._ensure_vectors()
        if not self.vectors:
            return []
        engine, _ = _try_load_embedder()
        if engine is None:
            return []
        query_vec = _embed_query(engine, query)
        if not query_vec:
            return []
        scores = _cosine_scores(query_vec, self.vectors)
        return _top_indices(scores, top_k)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """混合检索：BM25 ∪ 向量 → RRF 合并，返回最相关片段。"""
        self._prepare()
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []

        bm25_ranked = self._bm25_rank(query)
        dense_ranked = self._dense_rank(query)
        if dense_ranked:
            merged = _rrf_merge(bm25_ranked, dense_ranked, top_k)
        else:
            merged = bm25_ranked[:top_k]

        results = []
        for chunk_id in merged:
            chunk = self.chunks[chunk_id]
            result: dict = {
                "file": chunk["file"],
                "line": chunk["line"],
                "text": chunk["text"],
            }
            if "page" in chunk:
                result["page"] = chunk["page"]
            if "part" in chunk:
                result["part"] = chunk["part"]
            results.append(result)
        return results

    @property
    def notes(self) -> list[str]:
        return list(self._notes)


def _resolve_root(directory: str) -> Path:
    """解析到工作区/授权根内的目录（与工具读边界同一套运行期根，P1-B 对齐）。"""
    root = WORKSPACE_ROOT
    target = Path(directory).expanduser()
    if not target.is_absolute():
        target = WORKSPACE_ROOT / target
    else:
        # 绝对路径落在运行期 FileScope 授权根内时，以该根判定（支持严格 Project 的 wl 目录）
        try:
            from tools import _active_read_root_for

            root = _active_read_root_for(target)
        except Exception:
            root = WORKSPACE_ROOT
    target = target.resolve()
    # ⚠️ root 与 target 必须**对称规范化**（同 tools.py::_resolve_under_root）。
    # 真实触发条件是 Windows 8.3 短名：GitHub runner 上 `tempfile.gettempdir()`
    # 返回 `C:\Users\RUNNER~1\...`，测试把该路径赋给模块级 WORKSPACE_ROOT；
    # 而 `target.resolve()` 会把短名展开成长名 ⇒ 两侧字符串不等 ⇒ relative_to
    # 抛 ValueError ⇒ 把「明明在根下」的目录误判成越界（CI 上表现为工具被吞成
    # 'An error occurred while running the tool.'）。根不可解析时保持原样，
    # 交给下面的 relative_to 决定。
    try:
        root = Path(root).resolve()
    except OSError:
        pass
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"只能索引工作区 {root} 内的目录")
    if target.is_file():
        target = target.parent  # 传了文件路径时，自动退到文件所在目录
    if not target.exists() or not target.is_dir():
        raise ValueError(f"目录不存在：{target}")
    return target


def _load_or_build(root: Path, auto_max_files: int) -> RagIndex:
    # P1-3：并发构建互斥——多线程同时触发自动建索引时串行化 save()，
    # 避免半截 JSON 写盘被并发 load() 读到（save 本身已是原子写）。
    with _INDEX_WRITE_LOCK:
        index = RagIndex.load(root)
        if index is not None and not index.is_stale():
            return index
        count = sum(1 for _ in _iter_text_files(root, auto_max_files))
        if count >= auto_max_files:
            raise ValueError(
                f"目录 {root} 较大（至少 {auto_max_files} 个文本文件），"
                f"自动索引被跳过。请对更具体的子目录提问（在问题里说明在哪个文件夹找），"
                f"或先用 index_workspace 对目标子目录建索引。"
            )
        index = RagIndex(root)
        n_files, n_chunks = index.build(auto_max_files)
        if n_files == 0:
            raise ValueError(f"{root} 下没有可索引的文本文件")
        index.save()
    return index


def _search_note(index: RagIndex) -> str:
    """把检索模式说明附到结果里（向量不可用时给一句可操作提示）。"""
    if index.vectors:
        return "（本次为关键词+向量混合检索）"
    if not _embed_enabled():
        return ""
    model_dir = _resolve_embed_model_dir()
    return f"（向量模型未就绪，本次为关键词检索；把模型放到 {model_dir or EMBED_DEFAULT_DIR} 即可启用语义检索）"


@function_tool(failure_error_function=tool_failure_error_function)
def index_workspace(directory: str = ".", max_files: int = 800) -> str:
    """扫描本地文档并建立检索索引（RAG 第一步）。
    directory 是工作区内的目录（默认整个工作区）；max_files 是索引文件数上限。
    支持 .md/.txt/.py/.tsx/.json/.csv 等文本文件，自动跳过 .venv/.git/大文件等。
    若已配置本地向量模型，建索引时会顺带计算向量（首次会慢一些，属正常）。"""
    root = _resolve_root(directory)
    limit = max(1, min(int(max_files), 5000))
    with _INDEX_WRITE_LOCK:
        try:
            index = RagIndex(root)
            n_files, n_chunks = index.build(limit)
        except ValueError as exc:
            return f"索引失败：{exc}"
        if n_files == 0:
            return f"{root} 下没有可索引的文本文件。"
        index.save()
    mode = "关键词+向量混合" if index.vectors else "关键词"
    note = "\n".join(index.notes) if index.notes else ""
    tail = f"\n{note}" if note else ""
    return (
        f"索引完成：{n_files} 个文件、{n_chunks} 个片段（{mode}索引，目录 {root}）。"
        f"现在可以问我文档里的内容了。{tail}"
    )


@function_tool(failure_error_function=tool_failure_error_function)
def search_documents(query: str, directory: str = ".", top_k: int = 5) -> str:
    """在本地文档索引里检索最相关的片段（RAG 查询）。
    query 是自然语言问题；directory 指定在哪个目录里找（默认工作区根目录）；
    返回带文件路径和行号的原文片段，供你阅读后作答。首次使用会自动建索引。"""
    query = query.strip()
    if not query:
        return "错误：查询内容不能为空。"
    limit = max(1, min(int(top_k), 10))
    root = _resolve_root(directory)
    try:
        index = _load_or_build(root, DEFAULT_AUTO_FILES)
    except ValueError as exc:
        return f"检索失败：{exc}"
    results = index.search(query, limit)
    if not results:
        return (
            f"没有找到与“{query}”相关的片段，换个说法试试，"
            f"或先用 index_workspace 刷新索引。\n"
            f"（说明：不要再重复调用 search_documents 或 list_workspace_files 来找，"
            f"换个关键词或先建索引更有用。）"
        )
    lines = [
        f"目录: {root}",
        f"找到 {len(results)} 个相关片段（供阅读，可能有噪音，请综合判断）：",
    ]
    for i, item in enumerate(results, 1):
        if item.get("page"):
            where = f"第 {item['page']} 页"
        elif item.get("part"):
            where = str(item["part"])
        else:
            where = f"约第 {item['line']} 行"
        lines.append(f"\n{i}. {item['file']}（{where}）")
        lines.append(item["text"][:700])
    note = _search_note(index)
    if note:
        lines.append(f"\n{note}")
    lines.append(
        "\n（说明：相关片段已在上方，直接阅读后作答并注明文件即可，"
        "无需再调用 list_workspace_files 或重复 search_documents。）"
    )
    from runtime.trust import tag

    return tag("本地文档(RAG)", str(root), "\n".join(lines), max_len=24000)
