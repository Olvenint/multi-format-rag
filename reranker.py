"""
Rerank 精排（3.0 检索增强：把最相关的排到最前面）

原理（L05 讲义）：
    召回阶段放宽到 top-N（混合检索），精排阶段用更贵的模型逐对打分，砍到 top_k。
    Rerank 只改【排序】、不改【召回】——recall 是第一阶段（混合检索）的活。

实现（方案 ①：API Rerank）：
    使用阿里云百炼（DashScope）的 rerank 模型（默认 gte-rerank），
    与现有 DASHSCOPE_API_KEY 同一套 key 体系，无需新申请 key。

容错（熔断降级）：
    - 未安装新版 dashscope / API 调用失败 → 打印一次警告后降级为原排序
    - 失败一次后本进程内不再重试（_disabled 熔断），避免每次查询都白等超时
"""
from typing import List

from langchain_core.documents import Document


class Reranker:
    """
    交叉编码器风格的精排器（API 版）

    用法：
        reranker = Reranker(model="gte-rerank", enabled=True)
        top = reranker.rerank(query, candidate_docs, top_k=3)
    """

    def __init__(self, model: str = "gte-rerank", enabled: bool = True):
        self.model = model
        self.enabled = enabled
        self._disabled = False  # 熔断标志：失败一次后本进程内不再调用 API

    def rerank(self, query: str, docs: List[Document], top_k: int) -> List[Document]:
        """
        对候选 docs 逐对打分重排，返回最相关的 top_k 个。

        降级行为：任何失败（缺包/API 异常）→ 熔断 + 返回原顺序的 docs[:top_k]
        """
        if not self.enabled or self._disabled or not docs:
            return docs[:top_k]

        try:
            from dashscope import TextReRank

            response = TextReRank.call(
                model=self.model,
                query=query,
                documents=[d.page_content for d in docs],
            )
            results = sorted(
                response.output.results,
                key=lambda r: r.relevance_score,
                reverse=True,
            )
            return [docs[r.index] for r in results[:top_k]]
        except ImportError:
            self._disabled = True
            print("[Rerank] 未安装支持 TextReRank 的 dashscope，已降级为原排序"
                  "（可执行：pip install -U dashscope）")
        except Exception as e:
            self._disabled = True
            print(f"[Rerank] API 调用失败（{str(e)[:80]}），已降级为原排序"
                  "（可在 config_data.py 设 RERANK_ENABLED=False 关闭）")
        return docs[:top_k]
