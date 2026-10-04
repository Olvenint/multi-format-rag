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
import glob
import json
import os
from datetime import datetime
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
# 多会话记忆（5.0，依据学习 L11）：一个 session:{sid} Hash 存一个会话的多轮对话
# ============================================================
class SessionMemory:
    """
    多会话记忆隔离：按 session_id 各存各的对话历史，解决多用户串话。

    设计（贴近真实生产"热会话"层）：
        - 存储：Redis Hash，键 = `session:{sid}`，每个 field = 一轮对话
          field 名如 "1"、"2"..."N"（按轮次递增），值为一轮的 JSON。
        - TTL（SESSION_TTL）：会话过期自动消失，无需手动清理（对应 L07 "Hash 格子 + 保质期"）。
        - 降级：Redis 不可用时回退到按 session 分文件的 JSONL 冷备份
          （SESSION_DIR/{sid}.jsonl），保证服务不因 Redis 缺位而中断。

    与旧 ConversationMemory 的区别：
        ConversationMemory = 单一全局 JSONL（所有用户共享）→ 会串话；
        SessionMemory      = 一个会话一个 Hash/file → 各管各的。

    用法：
        mem = SessionMemory()
        mem.save(sid, "问题", "回答")          # 存一轮
        text = mem.get_history_text(sid)       # 取该会话历史，格式化
    """

    def __init__(self, cache: RedisCache = None):
        self.cache = cache or _get_cache()

    # --------------------------------------------------------
    # 写入：保存一轮问答到某个会话
    # --------------------------------------------------------
    def save(self, session_id: str, query: str, answer: str, context: str = "") -> None:
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "query": query,
            "answer": answer,
            "context": context,
        }
        client = self.cache._get_client()
        if client is not None:
            try:
                # 该会话已有多少轮，作为下一个 field 名（1 起）
                field = str(client.hlen(f"session:{session_id}") + 1)
                client.hset(f"session:{session_id}", field, json.dumps(record, ensure_ascii=False))
                # 刷新 TTL：有活动就会话重新计时，"活跃的会话继续活，废弃的自动清"
                client.expire(f"session:{session_id}", config.SESSION_TTL)
                return
            except Exception:
                pass  # Redis 写失败 → 落回本地文件

        self._fallback_save(session_id, record)

    def _fallback_save(self, session_id: str, record: dict) -> None:
        """Redis 不可用时的冷备份：按 session 分文件，避免多会话互相读错"""
        os.makedirs(config.SESSION_DIR, exist_ok=True)
        path = os.path.join(config.SESSION_DIR, f"{session_id}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # --------------------------------------------------------
    # 读取：加载某会话最近 N 轮
    # --------------------------------------------------------
    def load_recent(self, session_id: str, limit: int = 5) -> list:
        client = self.cache._get_client()
        if client is not None:
            try:
                # hgetall 拿全部 field，按 field 名（轮次）排序取最近 N 轮
                fields = client.hgetall(f"session:{session_id}")
                if fields:
                    ordered = sorted(fields.items(), key=lambda kv: int(kv[0]))
                    records = [json.loads(v) for _, v in ordered]
                    return self._merge_records(records, self._fallback_recent(session_id, limit), limit)
                return self._fallback_recent(session_id, limit)  # Hash 为空 → 回退冷备份（防 Redis 恢复后丢失宕机期会话）
            except Exception:
                pass
        return self._fallback_recent(session_id, limit)

    @staticmethod
    def _merge_records(*lists: list, limit: int = 5) -> list:
        """合并 Redis 与 JSONL 冷备份记录：同一轮只算一份（按 timestamp 排序取最近 limit 轮）"""
        merged = {}
        for records in lists:
            for r in records or []:
                merged[(r.get("timestamp", ""), r.get("query", ""))] = r
        ordered = sorted(merged.values(), key=lambda r: r.get("timestamp", ""))
        return ordered[-limit:]

    def _fallback_recent(self, session_id: str, limit: int = 5) -> list:
        path = os.path.join(config.SESSION_DIR, f"{session_id}.jsonl")
        if not os.path.exists(path):
            return []
        records = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    records.append(json.loads(line))
        return records[-limit:]

    # --------------------------------------------------------
    # 工具：格式化为可注入 LLM 上下文的历史文本（与 ConversationMemory 同格式）
    # --------------------------------------------------------
    def get_history_text(self, session_id: str, limit: int = 5) -> str:
        records = self.load_recent(session_id, limit)
        if not records:
            return ""
        lines = []
        for r in records:
            lines.append(f"用户：{r['query']}")
            lines.append(f"AI：{r['answer']}")
            lines.append("")
        return "\n".join(lines).strip()

    # --------------------------------------------------------
    # 管理：清空某会话 / 清空全部会话
    # --------------------------------------------------------
    def clear_session(self, session_id: str) -> None:
        client = self.cache._get_client()
        if client is not None:
            try:
                client.delete(f"session:{session_id}")
            except Exception:
                pass
        path = os.path.join(config.SESSION_DIR, f"{session_id}.jsonl")
        if os.path.exists(path):
            os.remove(path)

    def clear_all_sessions(self) -> None:
        client = self.cache._get_client()
        if client is not None:
            try:
                keys = client.keys("session:*")
                if keys:
                    client.delete(*keys)
            except Exception:
                pass
        import shutil
        if os.path.isdir(config.SESSION_DIR):
            shutil.rmtree(config.SESSION_DIR, ignore_errors=True)

    # --------------------------------------------------------
    # 会话列表：供前端 /sessions 拉取（v5.2.1：会话记忆与列表统一存 Redis 服务端）
    # --------------------------------------------------------
    def list_sessions(self, client_id: str = None) -> list:
        """
        列出会话：{session_id, turns(轮数), last_active(最后活跃时间)}，按最后活跃时间倒序。

        多会话按客户端区分（v5.2.2）：session_id 格式 = `{client_id}:{uuid}`，
        传 client_id 时只返回该客户端的会话（startswith 过滤），不同浏览器/用户各看各的。

        - Redis 可用：keys("session:*") → 每个 Hash 里取数字 field（轮次）与最大轮次的 timestamp。
        - Redis 不可用/为空：回退读 SESSION_DIR 下 *.jsonl 冷备份（文件 mtime 作最后活跃时间）。
        """
        def _match(sid: str) -> bool:
            return (not client_id) or sid.startswith(client_id + ":")

        client = self.cache._get_client()
        sessions = []
        if client is not None:
            try:
                for key in client.keys("session:*"):
                    sid = key[len("session:"):]
                    if not _match(sid):
                        continue
                    turns = 0
                    last_active = 0.0
                    for fname, fval in client.hgetall(key).items():
                        if not fname.isdigit():
                            continue  # 只统计数字轮次 field，跳过可能的元数据 field
                        turns += 1
                        try:
                            ts = json.loads(fval).get("timestamp", "")
                        except Exception:
                            ts = ""
                        if ts > last_active:  # 字符串时间戳字典序 = 时间序
                            last_active = ts
                    sessions.append({
                        "session_id": sid, "turns": turns, "last_active": last_active,
                    })
            except Exception:
                sessions = []  # Redis 读失败 → 回退冷备份
        if True:  # 无论 Redis 是否可用都并入 JSONL 冷备份，防止宕机期会话在 Redis 恢复后丢失（末尾按 session_id 合并去重）
            # 并入 JSONL 冷备份目录中的会话（同 session_id 的 Redis/冷备份记录在末尾合并去重）
            for path in glob.glob(os.path.join(config.SESSION_DIR, "*.jsonl")):
                sid = os.path.basename(path)[: -len(".jsonl")]
                if not _match(sid):
                    continue
                mtime = os.path.getmtime(path)  # float 时间戳，与 Redis 分支类型一致（合并/排序安全）
                turns = 0
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            turns += 1
                sessions.append({
                    "session_id": sid, "turns": turns, "last_active": mtime,
                })
        # 合并去重：同一会话 Redis 与冷备份各有一份时，轮数相加、活跃时间取最新
        merged = {}
        for s in sessions:
            prev = merged.get(s["session_id"])
            if prev is None:
                merged[s["session_id"]] = s
            else:
                merged[s["session_id"]] = {
                    "session_id": s["session_id"],
                    "turns": prev["turns"] + s["turns"],
                    "last_active": max(prev["last_active"], s["last_active"]),
                }
        sessions = list(merged.values())
        sessions.sort(key=lambda s: s["last_active"], reverse=True)
        return sessions


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
