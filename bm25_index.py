"""
BM25 词法检索索引（3.0 检索增强：混合检索的词法路）

作用：
    在 Chroma 向量检索之外，维护一份 BM25 倒排索引。
    查询时双路检索（向量 + BM25），再用 RRF 融合排名（见 qa_service.DocxRetriever）。

    与外部库（bm25s / rank_bm25）的区别：
        自己实现——公式与 L03 讲义完全一致（k1=1.5, b=0.75），
        代码可读、可教学、零额外依赖（jieba 可选：装了用 jieba 分词，
        没装自动降级为「中文按字切 + 英文按词切」，效果略差但能跑）。

BM25 公式（L03 讲义）：
    score(d, q) = Σ  IDF(t) × tf(t,d) × (k1 + 1) / (tf(t,d) + k1 × (1 - b + b × dl/avgdl))
    IDF(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))

    直觉：词越稀有越重要（IDF）× 词出现越多越相关（TF，k1 控制饱和）
          × 文档越短越可信（b 控制长度惩罚）
"""
import os
import re
import math
import pickle
from typing import Dict, List, Optional, Tuple

from langchain_core.documents import Document


# ============================================================
# 中文分词：优先 jieba，未安装时降级
# ============================================================
def tokenize(text: str) -> List[str]:
    """
    把文本切成词。中文场景推荐安装 jieba（pip install jieba）：
        "学习成绩怎么样" → ["学习", "成绩", "怎么样"]
    未安装时降级：中文按单字切、英文按词切（能跑但切分较粗）。
    """
    try:
        import jieba  # type: ignore
        return [t for t in jieba.cut(text) if t.strip()]
    except ImportError:
        tokens: List[str] = []
        for seg in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", text.lower()):
            if re.fullmatch(r"[\u4e00-\u9fff]+", seg):
                tokens.extend(list(seg))          # 中文按字切（降级方案）
            else:
                tokens.append(seg)                 # 英文/数字按词切
        return tokens


# ============================================================
# 核心类：BM25Index
# ============================================================
class BM25Index:
    """
    轻量 BM25 倒排索引（自实现，教学友好）

    用法：
        index = BM25Index().build(documents)          # 构建
        index.save(path)                              # 持久化
        index = BM25Index.load(path)                  # 加载
        hits = index.search("学习成绩 绩点", top_n=10)  # 检索，返回 [(Document, score)]
        BM25Index.rebuild_from_chroma(chroma, path)   # 从向量库全量重建
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1          # 词频饱和参数（L03：k1 越大 tf 影响越接近线性）
        self.b = b            # 长度惩罚参数（L03：b 越大长文档越吃亏）
        self.documents: List[Document] = []   # 原始 Document，按入库顺序
        self.doc_len: List[int] = []          # 每个 doc 的 token 数
        self.avgdl: float = 0.0               # 平均文档长度
        self.doc_freq: Dict[str, int] = {}    # term -> 出现在多少个文档 (df)
        self.postings: Dict[str, Dict[int, int]] = {}  # term -> {doc_idx: tf}
        self.idf: Dict[str, float] = {}       # term -> idf（build 时算好）

    # ----------------------------------------------------------
    # 构建 / 检索
    # ----------------------------------------------------------
    def build(self, documents: List[Document]) -> "BM25Index":
        """用一批文档重建索引（全量重建，数据量小够用）"""
        self.documents = list(documents)
        n = len(self.documents)

        self.doc_len = []
        self.doc_freq = {}
        self.postings = {}
        for idx, doc in enumerate(self.documents):
            terms = tokenize(doc.page_content or "")
            self.doc_len.append(len(terms))
            seen_in_doc = set()
            for t in terms:
                if t not in self.postings:
                    self.postings[t] = {}
                self.postings[t][idx] = self.postings[t].get(idx, 0) + 1
                seen_in_doc.add(t)
            for t in seen_in_doc:
                self.doc_freq[t] = self.doc_freq.get(t, 0) + 1

        self.avgdl = (sum(self.doc_len) / n) if n else 0.0
        self.idf = {}
        for term, df in self.doc_freq.items():
            # IDF = ln(1 + (N - df + 0.5) / (df + 0.5))，词越稀有 IDF 越大
            self.idf[term] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        return self

    def search(self, query: str, top_n: int = 10) -> List[Tuple[Document, float]]:
        """
        查询 → 返回 (Document, BM25 分数) 列表，按分数降序，最多 top_n 条。
        查询词在索引里完全没有 → 返回空列表（词法路无命中，交给向量路兜底）。
        """
        terms = tokenize(query)
        if not terms or not self.documents:
            return []

        scores: Dict[int, float] = {}
        for term in set(terms):  # 查询内同词只算一次
            for doc_idx in self.postings.get(term, {}):
                scores[doc_idx] = scores.get(doc_idx, 0.0) + self._term_weight(term, doc_idx)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [(self.documents[i], s) for i, s in ranked[:top_n] if s > 0]

    def _term_weight(self, term: str, doc_idx: int) -> float:
        """单文档单词的 BM25 贡献：IDF × TF 加权项"""
        tf = self.postings.get(term, {}).get(doc_idx, 0)
        if not tf:
            return 0.0
        dl = self.doc_len[doc_idx]
        denom = tf + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
        return self.idf.get(term, 0.0) * tf * (self.k1 + 1.0) / denom

    # ----------------------------------------------------------
    # 持久化
    # ----------------------------------------------------------
    def save(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @classmethod
    def load(cls, path: str) -> Optional["BM25Index"]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    @classmethod
    def rebuild_from_chroma(cls, chroma, save_path: str) -> "BM25Index":
        """
        从 Chroma 向量库全量读取文本重建索引并持久化。
        在 KnowledgeBase.upload_documents() 入库成功后调用，
        保证词法索引与向量库数据一致。
        """
        collection = chroma._collection
        data = collection.get(include=["documents", "metadatas"])
        docs: List[Document] = []
        for text, meta in zip(
            data.get("documents", []),
            data.get("metadatas", []) or [{}] * len(data.get("documents", [])),
        ):
            if text and text.strip():
                docs.append(Document(page_content=text, metadata=meta or {}))
        index = cls().build(docs)
        index.save(save_path)
        return index
