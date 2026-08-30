"""
对话记忆模块 —— 持久化保存用户与 AI 的每一次问答

存储格式：JSONL（每行一条完整的 JSON 记录）
    为什么选 JSONL？
        1. 追加写入不需要重写整个文件，效率高
        2. 逐行读取，内存友好，不会一次性加载全部
        3. 人类可读，方便调试和手动查看

每条记录结构：
    {
        "timestamp": "2026-08-06 14:30:00",
        "query": "客户接待流程是什么？",
        "answer": "客户接待流程包括...",
        "context": "【参考资料 1】\n标题：...\n内容：..."
    }

集成方式（在 docx_qa.py 中）：
    from memory import ConversationMemory

    class DocxQAService:
        def __init__(self):
            ...
            self.memory = ConversationMemory()    # ← 初始化记忆

        def ask(self, query):
            ...
            answer = self.chain.invoke(...)
            self.memory.save(query, answer, context)   # ← 保存本轮对话
            return answer
"""

# ============================================================
# 标准库
# ============================================================
import json                     # JSON 序列化 / 反序列化
import os                       # 文件路径判断、目录创建
from collections import deque   # 滑动窗口：固定长度队列，自动丢弃旧数据
from datetime import datetime   # 时间戳

# ============================================================
# 项目内配置
# ============================================================
import config_data as config


# ============================================================
# 核心类：ConversationMemory —— 对话记忆
# ============================================================
class ConversationMemory:
    """
    对话记忆管理器

    职责：
        1. 保存每一次用户提问和 AI 回答到 JSONL 文件
        2. 读取历史对话记录
        3. 将历史记录格式化为可注入上下文的文本

    使用示例：
        memory = ConversationMemory()
        memory.save("什么是RAG？", "RAG是检索增强生成...")
        history = memory.load_recent(config.MEMORY_WINDOW)           # 最近 config.MEMORY_WINDOW 条
        text = memory.get_history_text(limit=config.MEMORY_WINDOW)   # 格式化为文本
    """

    def __init__(self, file_path: str = None):
        """
        初始化记忆管理器

        参数：
            file_path: JSONL 存储路径，不传则用 config.MEMORY_PATH
        """
        self.file_path = file_path or config.MEMORY_PATH

        # 确保文件所在目录存在
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)

        # 如果文件不存在，创建空文件
        if not os.path.exists(self.file_path):
            with open(self.file_path, "w", encoding="utf-8") as f:
                pass

    # ============================================================
    # 写入：保存一条对话记录
    # ============================================================
    def save(self, query: str, answer: str, context: str = "") -> None:
        """
        将一轮问答追加写入 JSONL 文件

        参数：
            query:   用户问题
            answer:  AI 回答
            context: 检索到的上下文（可选，用于后期追溯）
        """
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "query": query,
            "answer": answer,
            "context": context,
        }

        with open(self.file_path, "a", encoding="utf-8") as f:
            # ensure_ascii=False 保证中文正常显示而非转义为 \uxxxx
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ============================================================
    # 读取：加载全部历史记录
    # ============================================================
    def load_all(self) -> list:
        """
        读取所有历史对话记录

        返回：
            [
                {"timestamp": "...", "query": "...", "answer": "...", "context": "..."},
                ...
            ]
        """
        records = []
        with open(self.file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    # ============================================================
    # 读取：加载最近 N 条记录
    # ============================================================
    def load_recent(self, limit: int = 5) -> list:
        """
        读取最近的 N 条对话记录（滑动窗口）

        什么是滑动窗口？
            只保留最近 N 轮对话，旧记录自动滑出窗口。
            好处：上下文长度恒定，不会随对话增长而膨胀。

        实现方式：用 deque(maxlen=limit) 遍历文件，
            队列满后自动丢弃最旧的行，无需把整个文件加载到内存。
            内存 O(limit)，而非 O(全部记录)。

        参数：
            limit: 窗口大小，返回最近 N 条记录

        返回：
            最近 N 条记录列表（按时间升序）
        """
        with open(self.file_path, "r", encoding="utf-8") as f:
            # deque(maxlen=limit)：队列满后自动丢弃最旧的行
            tail = deque(f, maxlen=config.MEMORY_WINDOW)
        return [json.loads(line) for line in tail if line.strip()]

    # ============================================================
    # 工具：格式化为可注入 LLM 上下文的历史文本
    # ============================================================
    def get_history_text(self, limit: int = 5) -> str:
        """
        将最近 N 条历史对话格式化为纯文本，可直接拼入 prompt

        输出格式：
            用户：什么是客户接待流程？
            AI：客户接待流程包括前台接待、需求沟通、服务介绍三个环节...

            用户：前台接待有哪些注意事项？
            AI：前台接待需要注意...

        参数：
            limit: 包含最近多少轮对话

        返回：
            格式化的历史对话文本；无历史时返回空字符串
        """
        records = self.load_recent(limit)
        if not records:
            return ""

        lines = []
        for r in records:
            lines.append(f"用户：{r['query']}")
            lines.append(f"AI：{r['answer']}")
            lines.append("")   # 空行分隔不同轮次

        return "\n".join(lines).strip()

    # ============================================================
    # 管理：清空记忆
    # ============================================================
    def clear(self) -> None:
        """清空所有对话记录"""
        with open(self.file_path, "w", encoding="utf-8") as f:
            pass

    # ============================================================
    # 管理：统计
    # ============================================================
    @property
    def count(self) -> int:
        """当前记忆中的对话轮数"""
        return len(self.load_all())


# ============================================================
# 测试入口
# ============================================================
if __name__ == "__main__":
    memory = ConversationMemory()

    # 模拟保存几轮对话
    memory.save("什么是RAG？", "RAG（检索增强生成）是一种结合信息检索和文本生成的技术...")
    memory.save("Chroma 是什么？", "Chroma 是一个开源的向量数据库，专门用于存储和检索嵌入向量...")
    memory.save("LangChain 的核心概念是什么？", "LangChain 的核心概念包括 Chain（链）、Agent（代理）和 Tool（工具）...")

    print(f"当前记忆轮数：{memory.count}")
    print(f"\n{'=' * 50}")
    print(f"最近 {config.MEMORY_WINDOW} 轮对话（格式化文本）：")
    print(f"{'=' * 50}")
    print(memory.get_history_text(limit=config.MEMORY_WINDOW))

    print(f"\n{'=' * 50}")
    print("JSON 原始记录：")
    print(f"{'=' * 50}")
    for r in memory.load_recent(config.MEMORY_WINDOW):
        print(f"[{r['timestamp']}] Q: {r['query']}")
        print(f"  A: {r['answer'][:50]}...")
        print()
