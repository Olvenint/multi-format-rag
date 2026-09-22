"""
入库自动术语提取（v4.2 新增：自动改写，替代手写词典）

背景：
    旧版 query_rewriter 用手写词典（触发词 → 扩展词），每换一个领域文档
    就要人工加词，文档量一大完全不可维护。
    本模块实现「入库即学习」：入库时对文档做 jieba 分词 + 词频统计，
    自动提取高频实词作为领域术语，写入 runtime/auto_dict.json。
    query_rewriter 查询时加载该词典自动改写，全程零手写。

流程：
    KnowledgeBase.upload_documents() 入库成功后
        → TermExtractor.update_terms(documents, source)   ← 本模块
        → runtime/auto_dict.json 更新（该 source 术语 + 全量合并去重）

与旧版（v3.0）区别：
    旧版：代码内 DEFAULT_DICT（写死）+ rewrite_dict.json（手写 JSON）
    新版：入库自动生成 auto_dict.json，随文档更新自动演进

存储格式（auto_dict.json）：
    {
        "updated_at": "2026-09-22 10:00:00",
        "sources": {"手册.docx": ["客户", "接待", ...], "简历.pdf": [...]},
        "all_terms": ["客户", "接待", "绩点", ...]   # 合并去重，按频次排序
    }
"""
import os
import re
import json
from collections import Counter
from typing import Dict, List

from langchain_core.documents import Document

import config_data as config
from bm25_index import tokenize

# ============================================================
# 提取参数
# ============================================================
TOP_TERMS_PER_SOURCE = 60          # 每个文件最多提取的术语数（太多噪声、太少覆盖不足）
MIN_TERM_LEN = 2                   # 术语最小字数（过滤单字：客户、接待 → ✓；的、了 → ✗）
MAX_TERM_LEN = 6                   # 术语最大字数（过滤超长专名/整句）
_CN_RE = re.compile(r"^[\u4e00-\u9fff]+$")   # 只保留纯中文词（英文/数字交给 embedding 兜底）

# 停用词：通用虚词/代词/疑问词/高频无义动词，提取术语时过滤
_STOPWORDS = {
    "一个", "我们", "你们", "他们", "她们", "它们", "这个", "那个", "这些", "那些",
    "什么", "怎么", "如何", "为什么", "可以", "进行", "相关", "以及", "或者",
    "没有", "不是", "就是", "自己", "目前", "需要", "如果", "因为", "所以",
    "通过", "对于", "关于", "由于", "其中", "同时", "但是", "然后", "之后",
    "以及", "已经", "将会", "可能", "应该", "主要", "重要", "一般", "非常",
}


# ============================================================
# 核心类：TermExtractor（入库自动术语提取）
# ============================================================
class TermExtractor:
    """
    从入库文档中自动提取领域高频实词，维护 runtime/auto_dict.json

    用法：
        extractor = TermExtractor()
        extractor.update_terms(documents, "手册.docx")   # 入库成功后调用

    说明：
        - 按 source 增量更新：同名文件重新入库时先移除旧术语再写新术语
        - all_terms 为全部 source 术语合并去重（保留频次排序），供改写使用
    """

    def __init__(self, path: str = None):
        self.path = path or config.AUTO_DICT_PATH

    # ----------------------------------------------------------
    # 词典读写
    # ----------------------------------------------------------
    def _load(self) -> dict:
        """读取 auto_dict.json；不存在或损坏时返回空结构"""
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "sources" in data:
                    return data
            except Exception:
                pass
        return {"updated_at": "", "sources": {}, "all_terms": []}

    def _save(self, data: dict):
        """原子写入：先写临时文件再替换，防止中断写坏词典"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ----------------------------------------------------------
    # 术语提取
    # ----------------------------------------------------------
    def extract_terms(self, documents: List[Document], top_n: int = TOP_TERMS_PER_SOURCE) -> List[str]:
        """
        对一批文档块做分词 + 词频统计，提取 Top N 高频实词

        过滤规则：
            1. 只保留 2~6 字纯中文词
            2. 剔除停用词
            3. 按词频降序取前 top_n 个（同频按字典序稳定排序）

        返回：
            按频次降序的术语列表
        """
        counter: Counter = Counter()
        for doc in documents:
            text = doc.page_content or ""
            for token in tokenize(text):
                token = token.strip()
                if not (MIN_TERM_LEN <= len(token) <= MAX_TERM_LEN):
                    continue
                if not _CN_RE.match(token):
                    continue
                if token in _STOPWORDS:
                    continue
                counter[token] += 1

        # 词频降序 + 同频按词排序（结果稳定可复现）
        return [w for w, _ in counter.most_common(top_n)]

    # ----------------------------------------------------------
    # 增量更新入口
    # ----------------------------------------------------------
    def update_terms(self, documents: List[Document], source: str):
        """
        提取当前文档术语并更新 auto_dict.json（入库成功后调用）

        参数：
            documents: 本次入库的 Document 列表
            source:    源文件名（如 "手册.docx"），用于增量覆盖

        返回：
            更新后的术语总数（all_terms 长度）
        """
        if not documents:
            return 0

        terms = self.extract_terms(documents)
        data = self._load()

        # 增量覆盖：同名文件重新入库 → 先移除旧术语再写入新术语
        if source in data["sources"]:
            del data["sources"][source]
        if terms:
            data["sources"][source] = terms

        # 合并全部 source 的术语（保持频次排序：首次出现顺序即频次序）
        merged: List[str] = []
        seen = set()
        for src_terms in data["sources"].values():
            for t in src_terms:
                if t not in seen:
                    merged.append(t)
                    seen.add(t)
        data["all_terms"] = merged

        from datetime import datetime
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save(data)
        return len(merged)


# ============================================================
# 便捷单例（与 KnowledgeBase 配合）
# ============================================================
_extractor = TermExtractor()


def update_terms(documents: List[Document], source: str) -> int:
    """便捷函数：入库成功后调用，更新自动术语词典"""
    return _extractor.update_terms(documents, source)


if __name__ == "__main__":
    # 测试入口：python term_extractor.py
    import sys
    from loader_factory import LoaderFactory

    path = sys.argv[1] if len(sys.argv) >= 2 else config.FILE_PATH
    if not path:
        print("用法：python term_extractor.py <文档路径>")
        sys.exit(1)

    loader = LoaderFactory.create(path)
    docs = loader.load()
    total = _extractor.update_terms(docs, os.path.basename(path))
    print(f"✅ 已提取术语，auto_dict 当前共 {total} 个")
    print(f"   词典位置：{config.AUTO_DICT_PATH}")
