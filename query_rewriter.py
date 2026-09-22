"""
查询改写（v4.2 自动词典版：解决词汇鸿沟）

原理（L04 讲义）：
    用户口语与知识库书面语无共同词时（如"学习成绩" vs "绩点/GPA"），
    任何匹配算法都对不上。解法：检索前把问题改写成知识库听得懂的话。
    注意：改写只影响【检索】，回答仍用原问题生成。

实现（自动词典，替代旧手写词典）：
    旧版（v3.0）：代码内 DEFAULT_DICT + 外部 rewrite_dict.json，均为手写，
                  每换一个领域文档就要人工加词，文档量大不可维护。
    新版（自动）：入库时 TermExtractor 自动提取文档高频实词，
                  写入 runtime/auto_dict.json（见 term_extractor.py）。
                  查询时对问题 jieba 分词，若某查询词不在术语库中、
                  但存在包含该词的文档术语（如"成绩" → "学习成绩"），
                  则追加该术语辅助检索。全程零手写、随文档自动演进。

能力边界（词级改写的天花板）：
    ✓ 口语短词 → 文档复合术语：查询"成绩"，术语库含"学习成绩" → 追加
    ✗ 口语 → 完全异词：查询"学习好不好"，文档写"GPA"（无共同子串）
      → 词级方案做不到，需语义级改写（LLM 改写 / HyDE），属 v5.0 演进方向

幂等性：
    查询词已在术语库中，或命中的术语已存在于查询中，则不再追加——
    保证 run_eval.py --rewrite 传入改写查询时不会二次改写。
"""
import json
import os
from typing import Dict, List

import config_data as config
from bm25_index import tokenize

# 查询词过滤：停用词/单字不参与改写匹配
_STOPWORDS = {
    "什么", "怎么", "如何", "为什么", "可以", "进行", "相关", "以及",
    "没有", "不是", "就是", "自己", "目前", "需要", "如果", "因为",
    "所以", "通过", "对于", "关于", "其中", "同时", "已经", "将会",
    "可能", "应该", "主要", "重要", "一般", "非常", "的", "了", "在",
}
MAX_EXPAND_PER_TOKEN = 3   # 每个查询词最多追加 3 个术语（防止改写过长冲淡检索）


# ============================================================
# 核心类：QueryRewriter
# ============================================================
class QueryRewriter:
    """
    自动词典查询改写器

    用法：
        rw = QueryRewriter(enabled=True)
        rw.rewrite("他的学习成绩怎么样？")
        # → "他的学习成绩怎么样？ 学习成绩"（若术语库含"学习成绩"）
        # 词表为空（未入库过文档）时返回原查询

    注意：
        术语库（runtime/auto_dict.json）由 TermExtractor 在入库时自动生成，
        本类不负责提取，只负责加载与匹配。
    """

    def __init__(self, dict_path: str = None, enabled: bool = True):
        self.enabled = enabled
        self.dict_path = dict_path or getattr(config, "AUTO_DICT_PATH", None)
        self.term_set: set = set()
        self.term_list: List[str] = []
        self._load_terms()

    def _load_terms(self):
        """加载自动术语库（auto_dict.json 的 all_terms）；缺失/损坏时为空集合"""
        if not self.dict_path or not os.path.exists(self.dict_path):
            return
        try:
            with open(self.dict_path, encoding="utf-8") as f:
                data = json.load(f)
            terms = data.get("all_terms", []) if isinstance(data, dict) else []
            # 只保留长度 ≥ 2 的术语（单字噪声不参与子串匹配）
            self.term_list = [t for t in terms if isinstance(t, str) and len(t) >= 2]
            self.term_set = set(self.term_list)
        except Exception:
            self.term_set = set()
            self.term_list = []

    def _expand_tokens(self, query: str) -> List[str]:
        """
        核心改写逻辑：对查询分词，找出「不在术语库中、但可作为文档术语子串」的查询词

        规则：
            1. 查询词已在术语库 → 跳过（查询已是文档语言，无扩展价值）
            2. 查询词不在术语库 → 在术语列表中找包含它的术语（子串匹配）
               （如查询"成绩" → 术语"学习成绩""课程成绩"）
            3. 追加的术语已存在于原查询中 → 跳过（幂等）

        返回：
            去重后的待追加术语列表（保持术语库频次顺序，每词最多 3 个）
        """
        additions: List[str] = []
        for q in tokenize(query):
            q = q.strip()
            if len(q) < 2 or q in _STOPWORDS:
                continue
            if q in self.term_set:
                continue  # 查询已用文档语言，无需扩展

            # 子串匹配：找包含该查询词的文档术语（按术语库频次顺序截取前 N 个）
            hits = [t for t in self.term_list if q in t and t not in query]
            for h in hits[:MAX_EXPAND_PER_TOKEN]:
                if h not in additions:
                    additions.append(h)

        return additions

    def rewrite(self, query: str) -> str:
        """
        改写查询：命中即追加文档术语（原查询保留，不替换）

        返回：
            改写后的查询字符串；无命中或词表为空时返回原查询（幂等）
        """
        if not self.enabled or not query or not self.term_list:
            return query

        additions = self._expand_tokens(query)
        if not additions:
            return query
        return f"{query} {' '.join(additions)}"


if __name__ == "__main__":
    # 测试入口：python query_rewriter.py "你的问题"
    import sys

    rw = QueryRewriter(enabled=True)
    q = sys.argv[1] if len(sys.argv) >= 2 else "他的学习成绩怎么样？"
    print(f"原查询：{q}")
    print(f"改写后：{rw.rewrite(q)}")
