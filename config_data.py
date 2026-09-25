"""
RAG 系统配置（支持多格式：docx / pdf / txt / excel）
"""
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 运行时目录：所有自动生成的文件都放在这里，与源代码分离
RUNTIME_DIR = os.path.join(_BASE_DIR, "runtime")

# 向量数据库存储目录
PERSIST_DIRECTORY = os.path.join(RUNTIME_DIR, "chroma_db")
# 数据库集合名（通用，不再绑定 docx）
COLLECTION_NAME = "rag_knowledge_base"
# 嵌入模型
EMBEDDING_MODEL = "text-embedding-v4"
# MD5 记录文件（文件级去重）
MD5_PATH = os.path.join(RUNTIME_DIR, "file_md5.txt")

# 对话记忆文件（JSONL 格式，每条一行）
MEMORY_PATH = os.path.join(RUNTIME_DIR, "conversation_memory.jsonl")

# 图片等提取资源的输出目录（各 Loader 默认使用）
OUTPUT_DIR = os.path.join(RUNTIME_DIR, "output")

# 检索配置
TOP_K = 3                    # 最终返回给 LLM 的文档数

# ============================================================
# 检索增强配置（3.0：查询改写 + 混合检索 + Rerank）
# ============================================================
# --- 查询改写（L04，v4.2 自动词典版：入库自动提取术语，零手写）---
REWRITE_ENABLED = True                                          # 查询改写开关
AUTO_DICT_PATH = os.path.join(RUNTIME_DIR, "auto_dict.json")    # 自动术语词典（入库时 TermExtractor 生成）

# --- 混合检索（L03：向量 + BM25 双路 → RRF 融合）---
HYBRID_ENABLED = True                                           # 混合检索开关
HYBRID_TOP_N = 10                                               # 各路检索候选数（融合前放宽）
BM25_INDEX_PATH = os.path.join(RUNTIME_DIR, "bm25_index.pkl")   # BM25 索引持久化路径
BM25_K1 = 1.5                                                   # BM25 词频饱和参数（默认值）
BM25_B = 0.75                                                   # BM25 长度惩罚参数（默认值）
RRF_K = 60                                                      # RRF 融合常数（L03 默认 60）

# --- Rerank 精排（L05，API 版，失败自动熔断降级）---
RERANK_ENABLED = True                                           # Rerank 开关
RERANK_MODEL = "gte-rerank"                                     # 百炼 rerank 模型
RERANK_TOP_N = 10                                               # 送给 Rerank 的候选数

# ============================================================
# 入库性能配置（4.0：批量 + 并行，依据学习 L06）
# ============================================================
INGEST_IMAGE_WORKERS = 4    # 图片描述并行线程数（IO 密集，4~8 为宜；过大易触发 API 限流）
INGEST_BATCH_SIZE = 32      # 向量化批量提交批次大小（每批一次 add_texts 往返）

# ============================================================
# Redis 缓存配置（4.3：两级缓存，依据学习 L07）
# ============================================================
REDIS_ENABLED = True                    # 总开关：False 时完全不用 Redis（直接走检索+生成）
REDIS_HOST = "127.0.0.1"                # Redis 地址（本机默认）
REDIS_PORT = 6379                       # Redis 端口（默认 6379）
REDIS_DB = 0                            # Redis 数据库编号（默认 0）
REDIS_PASSWORD = None                   # 密码（本机默认无密码）
REDIS_SOCKET_TIMEOUT = 1.0              # 连接超时（秒）：超时即熔断降级，不阻塞问答
RETRIEVAL_CACHE_TTL = 86400             # 检索缓存 TTL（秒，默认 24h；文档更新时会主动清空）
ANSWER_CACHE_TTL = 86400                # 回答缓存 TTL（秒，默认 24h；文档更新时会主动清空）

# 记忆配置
MEMORY_WINDOW = 5            # 滑动窗口大小：只保留最近 N 轮对话

# 聊天模型（问答用）
CHAT_MODEL = "deepseek-v4-pro"

# 视觉模型（图片转文字用）
VISION_MODEL = "qwen-vl-max"

# 测试文件路径（命令行测试时使用，None 则需通过参数传入）
FILE_PATH = None
# 示例：FILE_PATH = r"C:\path\to\your\document.docx"
