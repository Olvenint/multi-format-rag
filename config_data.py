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
TOP_K = 3                    # 检索返回的文档数（多格式后适当增大）

# 记忆配置
MEMORY_WINDOW = 5            # 滑动窗口大小：只保留最近 N 轮对话

# 聊天模型（问答用）
CHAT_MODEL = "deepseek-v4-pro"

# 视觉模型（图片转文字用）
VISION_MODEL = "qwen-vl-max"

# 测试文件路径（命令行测试时使用，None 则需通过参数传入）
FILE_PATH = None
# 示例：FILE_PATH = r"C:\path\to\your\document.docx"
