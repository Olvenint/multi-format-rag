"""
Redis 两级缓存（v4.3：检索缓存 + 回答缓存，依据学习 L07）

背景：
    每次问答都完整执行「查询改写 → 检索（embedding/BM25/Rerank）→ LLM 生成」，
    重复提问会重复花钱（DeepSeek 最贵、embedding/rerank 次之）、重复耗时。
    Redis 把高频问题的中间/最终结果缓存起来：
        检索缓存 → 省 embedding + rerank 的 API 调用
        回答缓存 → 省 DeepSeek 生成调用（最贵的一环）

两级缓存设计（用户决策 C）：
    1. 检索缓存  key = retrieval:{原问题}  → 检索三元组 (docs, image_descriptions, image_paths)
    2. 回答缓存  key = answer:{原问题}    → 最终回答字符串
    键用【改写前原问题】（用户决策 B）：
        查询改写会追加术语（如"他成绩怎么样" → "他成绩怎么样 学习成绩"），
        但缓存键只看原文——改写前后命中同一条缓存，不重复浪费。

失效策略（用户决策）：
    - TTL 自动过期（config 可配，默认 24h）
    - 文档重新入库时 invalidate_all() 清空全部缓存（内容变了，旧检索/旧回答全部失效，
      否则会出现"改了文档但回答还是旧的"的坑）

降级策略（用户决策 A）：
    Redis 未启动 / 连接失败 / 库不可用时自动【熔断降级】为直连：
    首次失败打印一次警告，后续直接跳过缓存，问答照常工作（与 Rerank 熔断同思路）。
    可通过 config.REDIS_ENABLED = False 完全关闭。

说明：
    - 回答缓存只在【无对话历史】时读写（多轮追问的回答依赖历史上下文，
      缓存单轮答案会导致答非所问；详见 qa_service.DocxQAService）。
    - 检索缓存与历史无关（检索只依赖问题本身），始终生效。
"""
import json
from typing import List, Optional, Tuple

import config_data as config
from langchain_core.documents import Document


class RedisCache:
    """Redis 两级缓存封装：惰性连接 + 熔断降级 + 序列化"""

    def __init__(self):
        self._client = None          # redis 客户端（惰性创建）
        self._available = None       # None=未探测，True/False=探测结果
        self._warned = False         # 是否已打印过降级警告（只打一次）

    # ============================================================
    # 连接管理
    # ============================================================
    def _get_client(self):
        """惰性获取 redis 客户端；不可用时返回 None 并熔断降级"""
        if config.REDIS_ENABLED is False:
            return None
        if self._available is False:
            return None
        if self._client is None:
            try:
                import redis  # 延迟导入：没装 redis-py 也不影响主流程
                self._client = redis.Redis(
                    host=config.REDIS_HOST,
                    port=config.REDIS_PORT,
                    db=config.REDIS_DB,
                    password=config.REDIS_PASSWORD,
                    decode_responses=True,      # 返回 str 而非 bytes
                    socket_timeout=config.REDIS_SOCKET_TIMEOUT,  # 超时即降级，不阻塞问答
                )
                self._client.ping()             # 真实探活：连接失败会抛异常
                self._available = True
            except Exception:
                self._available = False
                self._warn_once()
                return None
        return self._client

    def _warn_once(self):
        """熔断降级提示（只打印一次，不刷屏）"""
        if not self._warned:
            print("[缓存] Redis 不可用（未启动？），已自动降级为直连，问答不受影响"
                  "（可在 config_data.py 设 REDIS_ENABLED=False 关闭）")
            self._warned = True

    # ============================================================
    # 检索缓存：query → (docs, image_descriptions, image_paths)
    # ============================================================
    def get_retrieval(self, query: str) -> Optional[Tuple[List[Document], str, List[str]]]:
        """读检索缓存；未命中 / Redis 不可用返回 None"""
        client = self._get_client()
        if client is None:
            return None
        try:
            raw = client.get(f"retrieval:{query}")
            if not raw:
                return None
            data = json.loads(raw)
            docs = [
                Document(page_content=d["page_content"], metadata=d.get("metadata", {}))
                for d in data["docs"]
            ]
            return docs, data["image_descriptions"], data["image_paths"]
        except Exception:
            # 反序列化异常当作未命中，不阻断主流程
            return None

    def set_retrieval(
        self,
        query: str,
        docs: List[Document],
        image_descriptions: str,
        image_paths: List[str],
    ) -> None:
        """写检索缓存（docs 转 JSON 兼容结构再存）"""
        client = self._get_client()
        if client is None:
            return
        try:
            payload = json.dumps({
                "docs": [
                    {"page_content": d.page_content, "metadata": d.metadata}
                    for d in docs
                ],
                "image_descriptions": image_descriptions,
                "image_paths": image_paths,
            }, ensure_ascii=False)
            client.set(f"retrieval:{query}", payload, ex=config.RETRIEVAL_CACHE_TTL)
        except Exception:
            pass  # 缓存写失败不影响主流程

    # ============================================================
    # 回答缓存：query → answer
    # ============================================================
    def get_answer(self, query: str) -> Optional[str]:
        """读回答缓存；未命中 / Redis 不可用返回 None"""
        client = self._get_client()
        if client is None:
            return None
        try:
            return client.get(f"answer:{query}")
        except Exception:
            return None

    def set_answer(self, query: str, answer: str) -> None:
        """写回答缓存"""
        client = self._get_client()
        if client is None:
            return
        try:
            client.set(f"answer:{query}", answer, ex=config.ANSWER_CACHE_TTL)
        except Exception:
            pass

    # ============================================================
    # 失效：文档更新时清空全部缓存
    # ============================================================
    def invalidate_all(self) -> bool:
        """清空两级缓存（文档内容变了，旧检索/旧回答全部失效）；返回是否成功"""
        client = self._get_client()
        if client is None:
            return False
        try:
            # 按前缀批量删除 retrieval:* 与 answer:*
            keys = []
            for prefix in ("retrieval:*", "answer:*"):
                keys.extend(client.keys(prefix))
            if keys:
                client.delete(*keys)
            return True
        except Exception:
            return False


# ============================================================
# 模块级便捷函数（供 knowledge_base / qa_service 直接调用，无需实例化）
# ============================================================
_cache = None


def _get_cache() -> RedisCache:
    global _cache
    if _cache is None:
        _cache = RedisCache()
    return _cache


def get_retrieval(query: str):
    return _get_cache().get_retrieval(query)


def set_retrieval(query: str, docs, image_descriptions: str, image_paths) -> None:
    _get_cache().set_retrieval(query, docs, image_descriptions, image_paths)


def get_answer(query: str) -> Optional[str]:
    return _get_cache().get_answer(query)


def set_answer(query: str, answer: str) -> None:
    _get_cache().set_answer(query, answer)


def invalidate_all() -> bool:
    """入库后清空全部缓存（内容变了，旧答案失效）"""
    return _get_cache().invalidate_all()


# ============================================================
# 测试入口：python redis_cache.py
# ============================================================
if __name__ == "__main__":
    print("=== Redis 缓存自测（需要本机 Redis 已启动）===")
    cache = RedisCache()
    if cache._get_client() is None:
        print("Redis 不可用：请先启动 Redis 服务（如 redis-server.exe），本模块将自动降级。")
    else:
        # 写读检索缓存
        test_doc = Document(page_content="客户接待流程测试", metadata={"heading": "测试"})
        cache.set_retrieval("测试问题", [test_doc], "图片描述", ["a.png"])
        docs, desc, paths = cache.get_retrieval("测试问题")
        print(f"检索缓存 OK：{docs[0].page_content} | {desc} | {paths}")
        # 写读回答缓存
        cache.set_answer("测试问题", "这是缓存答案")
        print(f"回答缓存 OK：{cache.get_answer('测试问题')}")
        # 清空
        cache.invalidate_all()
        print(f"清空后检索缓存：{cache.get_retrieval('测试问题')}")
        print("自测通过 ✅")
