"""
docx 文档 AI 问答服务 —— 在线流程（向量检索 + 预计算描述 + LLM 回答）

完整调用链：
    用户提问
      → ConversationMemory.get_history_text()  取最近 N 轮历史对话
      → DocxRetriever.retrieve()                从 Chroma 向量库检索 top_k 个文档片段
                                                 + 读取入库时预计算的 image_description
      → DocxQAService._format_context()          将历史 + 文本片段 + 图片描述拼成上下文
      → ChatDeepSeek + ChatPromptTemplate         把上下文+问题发给大模型生成回答
      → StrOutputParser                          解析为纯字符串返回
      → ConversationMemory.save()                将本轮问答持久化到 conversation_memory.jsonl

图片描述策略（企业级做法）：
    入库阶段：DocxKnowledgeBase.upload_documents() 对每张图片调用 qwen-vl-max，
              将描述存入 metadata["image_description"]，一次计算永久使用。
    查询阶段：DocxRetriever.retrieve() 直接读取 metadata["image_description"]，
              零次视觉 API 调用，查询速度从分钟级降到秒级。
"""

# ============================================================
# 标准库
# ============================================================
import os                       # 文件路径拼接、判断文件是否存在
import base64                   # 把图片二进制编码为 base64 字符串，方便发给视觉 API
from typing import List         # 类型注解，标明参数/返回值是列表

# ============================================================
# LangChain 核心组件
# ============================================================
from langchain_chroma import Chroma
#   ↑ Chroma 向量数据库的 LangChain 封装，支持持久化、相似度检索

from langchain_community.embeddings import DashScopeEmbeddings
#   ↑ 阿里云 DashScope 的文本嵌入模型（text-embedding-v4），把文本转为 1536 维浮点向量

from langchain_core.documents import Document
#   ↑ LangChain 的文档对象，两个核心属性：page_content（文本）、metadata（元数据字典）

from langchain_core.prompts import ChatPromptTemplate
#   ↑ 聊天提示词模板，支持 system / user / assistant 多角色消息

from langchain_core.output_parsers import StrOutputParser
#   ↑ 输出解析器，把 AIMessage 对象转为纯字符串

from langchain_deepseek import ChatDeepSeek
#   ↑ DeepSeek 聊天模型（deepseek-chat），用于最终生成回答

# ============================================================
# 项目内模块
# ============================================================
import config_data as config
from memory import ConversationMemory
#   数据库路径（PERSIST_DIRECTORY）、表名（COLLECTION_NAME）、
#   嵌入模型名（EMBEDDING_MODEL）、检索数量（TOP_K）、
#   聊天模型（CHAT_MODEL）、视觉模型（VISION_MODEL）等


# ============================================================
# 工具函数：图片 → base64 编码
# ============================================================
def _image_to_base64(image_path: str) -> str:
    """
    把本地图片文件编码为 base64 data URL

    为什么要 base64？
        视觉 API（如 qwen-vl-max）接受两种图片传参方式：
            1. 公网可访问的 URL（需要把图片上传到 OSS/图床）
            2. base64 编码的内联数据（直接从本地读取，无需上传）
        显然方式 2 更适合本离线场景——直接读二进制编码就行。

    参数：
        image_path: 本地图片绝对路径（如 output/images/image_0001.png）

    返回：
        "data:image/png;base64,iVBORw0KGg..."  格式的 data URL
    """
    ext = os.path.splitext(image_path)[-1].lower().lstrip(".")
    mime_map = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "bmp": "bmp"}
    mime = mime_map.get(ext, "png")   # 未知后缀统一当 png 处理

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/{mime};base64,{b64}"


# ============================================================
# 工具函数：单张图片 → 文字描述（DashScope 视觉模型）
# ============================================================
# 给视觉模型的提示词——要求简洁，控制在 100 字，防止描述过长冲淡文本上下文
_VISION_PROMPT = (
    "请用简洁的中文描述这张图片的内容，包括图片中展示的关键信息、"
    "数据、流程或结构。控制在100字以内。"
)


def _describe_single_image(img_path: str) -> str:
    """
    调用 qwen-vl-max 描述单张图片，返回纯文字描述

    职责：只描述一张图，不做拼接格式化。
    被两处调用：
        1. describe_images() —— 批量描述时逐张调用
        2. DocxKnowledgeBase.upload_documents() —— 入库时预计算

    参数：
        img_path: 图片绝对路径

    返回：
        纯文字描述字符串；失败时返回错误提示
    """
    if not os.path.exists(img_path):
        return f"图片文件不存在 ({img_path})"

    # 延迟导入：不是所有场景都会用到视觉模型
    try:
        from dashscope import MultiModalConversation
    except ImportError:
        return "[图片描述失败：请安装 dashscope 包]"

    b64_url = _image_to_base64(img_path)
    messages = [{
        "role": "user",
        "content": [
            {"image": b64_url},
            {"text": _VISION_PROMPT}
        ]
    }]

    try:
        response = MultiModalConversation.call(
            model=config.VISION_MODEL,
            messages=messages,
        )
        return response.output.choices[0].message.content[0]["text"]
    except Exception as e:
        return f"描述失败 ({str(e)[:50]})"


# ============================================================
# 工具函数：批量图片 → 文字描述（兼容旧接口）
# ============================================================
def describe_images(image_paths: List[str]) -> str:
    """
    批量描述图片，拼接成 "图片 1：xxx\\n图片 2：yyy" 格式

    企业级做法：入库时已预计算，查询时不再调用此函数。
    保留是为了兼容直接调用的场景。

    参数：
        image_paths: 图片文件路径列表

    返回：
        拼接后的文字描述字符串；无图片时返回空字符串
    """
    if not image_paths:
        return ""

    descriptions = []
    for i, img_path in enumerate(image_paths):
        desc = _describe_single_image(img_path)
        descriptions.append(f"图片 {i + 1}：{desc}")

    return "\n".join(descriptions)


# ============================================================
# 核心类 1：DocxRetriever —— 检索 + 图片解析
# ============================================================
class DocxRetriever:
    """
    从 Chroma 向量库检索相关文档片段，读取预计算的图片描述

    位置：在线流程的第一步
    职责：
        1. 把用户问题向量化 → 在 Chroma 中做相似度检索
        2. 检查命中文档的 metadata["image_description"] 字段（入库时预计算）
        3. 返回 (文档列表, 图片描述, 图片路径) 三元组

    企业级做法：图片描述在入库时已由 DocxKnowledgeBase 预计算并存入
    metadata["image_description"]，查询时直接读取，零次视觉 API 调用。

    使用示例：
        retriever = DocxRetriever(top_k=3)
        docs, img_desc, img_paths = retriever.retrieve("客户接待流程是什么？")
    """

    def __init__(self, top_k: int = None):
        """
        初始化检索器

        参数：
            top_k: 返回的文档数量，不传则用 config.TOP_K（默认 2）
        """
        self.top_k = top_k or config.TOP_K

        # 连接 Chroma 向量数据库（会自动从 persist_directory 加载已有数据）
        self.chroma = Chroma(
            collection_name=config.COLLECTION_NAME,        # 表名：docx_rag
            embedding_function=DashScopeEmbeddings(        # 嵌入模型：把查询文本向量化
                model=config.EMBEDDING_MODEL               # text-embedding-v4
            ),
            persist_directory=config.PERSIST_DIRECTORY,    # 持久化目录：runtime/chroma_db/
        )

        # 创建检索器：内部调用 chroma.similarity_search(query, k=top_k)
        self.retriever = self.chroma.as_retriever(
            search_kwargs={"k": self.top_k}
        )

    def retrieve(self, query: str) -> tuple:
        """
        执行向量检索，读取预计算的图片描述

        返回值三元组说明：
            docs:             List[Document]
                page_content    → 文档文本内容
                metadata        → 标题、章节、图片路径、预计算描述等

            image_descriptions: str
                入库时预计算的图片描述（从 metadata["image_description"] 读取）
                如："图片 1：这是一张客户接待流程图...\n图片 2：这是一张表格..."

            all_image_paths:    List[str]
                所有关联图片的本地绝对路径（去重后）

        参数：
            query: 用户自然语言问题
        """
        # ① 向量检索：query 被向量化后，和数据库中的文档向量做余弦相似度计算
        docs: List[Document] = self.retriever.invoke(query)

        # ② 收集图片路径 + 读取预计算描述
        all_image_paths = []
        seen = set()
        desc_parts = []
        for doc in docs:
            # 收集图片路径（去重）
            images = doc.metadata.get("images", [])
            if isinstance(images, str):
                images = [images]
            for path in images:
                if path and path not in seen:
                    all_image_paths.append(path)
                    seen.add(path)

            # 直接读取入库时预计算的图片描述，零次视觉 API 调用
            desc = doc.metadata.get("image_description", "")
            if desc:
                desc_parts.append(desc)

        image_descriptions = "\n".join(desc_parts)

        return docs, image_descriptions, all_image_paths


# ============================================================
# 核心类 2：DocxQAService —— 问答编排
# ============================================================
class DocxQAService:
    """
    将「检索」、「格式化上下文」、「LLM 回答」串联成完整的问答服务

    位置：在线流程的顶层入口
    职责：
        1. 持有 DocxRetriever —— 负责检索
        2. 持有 ChatPromptTemplate —— 负责构造提示词
        3. 持有 ChatDeepSeek —— 负责生成回答
        4. 持有 ConversationMemory —— 负责持久化每轮对话
        5. 用 LangChain 链式调用 (LCEL) 把上面三步串起来：
           prompt | model | parser

    使用示例：
        qa = DocxQAService()
        answer = qa.ask("顾问手册中的客户接待标准是什么？")
        print(answer)
    """

    def __init__(self):
        # ---------- 检索器 ----------
        self.retriever = DocxRetriever()

        # ---------- 记忆 ----------
        self.memory = ConversationMemory()

        # ---------- 提示词模板 ----------
        # system 消息：定义 AI 的角色和行为规范
        # user 消息：把 context（参考资料）+ query（用户问题）拼接在一起
        self.prompt_template = ChatPromptTemplate.from_messages([
            ("system",
             "你是一个专业的文档问答助手。请根据提供的参考资料，简洁准确地回答用户问题。\n"
             "规则：\n"
             "1. 以参考资料为主要依据，不要凭空编造\n"
             "2. 如果参考资料包含图片描述，请结合图片信息回答\n"
             "3. 如果参考资料不足以回答问题，请如实告知\n"
             "4. 答案要结构化、条理清晰，简洁明了，控制在200字以内"),
            ("user",
             "参考资料：\n"
             "{context}\n\n"              # ← 检索到的文档文本 + 图片描述
             "用户问题：{query}")          # ← 用户原始问题
        ])

        # ---------- 聊天模型 ----------
        self.chat_model = ChatDeepSeek(
            model_name=config.CHAT_MODEL  # deepseek-v4-pro
        )

        # ---------- 链式调用 (LCEL: LangChain Expression Language) ----------
        # "|" 是管道操作符，数据从左到右依次传递：
        #   ChatPromptTemplate → 把 context 和 query 填入模板，生成 PromptValue
        #                     → ChatDeepSeek 调用 API，生成 AIMessage
        #                     → StrOutputParser 解析为纯字符串
        self.chain = self.prompt_template | self.chat_model | StrOutputParser()

    # ============================================================
    # 私有方法：格式化上下文
    # ============================================================
    def _format_context(
        self,
        docs: List[Document],
        image_descriptions: str,
        image_paths: List[str],
        history_text: str = ""
    ) -> str:
        """
        把检索到的文档片段、图片描述和历史对话拼成一条结构化上下文字符串

        拼接格式：
            【历史对话】
            用户：什么是客户接待流程？
            AI：客户接待流程包括...

            【参考资料 1】
            标题：客户接待基本流程
            所属章节：第三章 接待规范
            内容：客户到访后，前台应先...

            【参考资料 2】
            ...

            【关联图片描述】（共 3 张）
            图片 1：这是一个流程图，展示了...
            图片 2：这是一张表格，记录了...

        参数：
            docs:               检索到的文档片段
            image_descriptions: 图片文字描述（可能为空）
            image_paths:        图片路径列表
            history_text:       历史对话文本（滑动窗口，可能为空）

        返回：
            格式化的上下文字符串
        """
        if not docs and not image_descriptions and not history_text:
            return "无相关参考资料"

        parts = []

        # 历史对话（滑动窗口记忆）——让 LLM 能理解上下文连贯的追问
        if history_text:
            parts.append(f"【历史对话】\n{history_text}\n")

        # 拼接文档片段
        for i, doc in enumerate(docs):
            heading = doc.metadata.get("heading", "无标题")
            chapter = doc.metadata.get("chapter", "")

            # 构造单条参考资料的格式化文本
            part = f"【参考资料 {i + 1}】\n标题：{heading}\n"
            if chapter:
                part += f"所属章节：{chapter}\n"
            part += f"内容：{doc.page_content}\n"
            parts.append(part)

        # 追加图片描述（如果有的话）
        if image_descriptions:
            img_count = len(image_paths) if image_paths else 0
            parts.append(
                f"\n【关联图片描述】（共 {img_count} 张）\n"
                f"{image_descriptions}\n"
            )

        return "\n".join(parts)

    # ============================================================
    # 公开方法：问答入口
    # ============================================================
    def ask(self, query: str) -> str:
        """
        一站式问答入口

        内部流程：
            ① memory.get_history_text()
               → 历史对话文本（滑动窗口，最近 N 轮）

            ② DocxRetriever.retrieve(query)
               → docs, image_descriptions, image_paths

            ③ _format_context(docs, image_descriptions, image_paths, history_text)
               → str 格式化的上下文字符串（历史对话 + RAG 检索结果 + 图片描述）

            ④ self.chain.invoke({"context": context, "query": query})
               → LLM 生成的回答字符串

            ⑤ self.memory.save(query, answer, context)
               → 持久化保存本轮问答记录

        参数：
            query: 用户自然语言问题

        返回：
            AI 助手的回答
        """
        # 步骤 1：获取历史对话（滑动窗口，只取最近 N 轮）
        history_text = self.memory.get_history_text(limit=config.MEMORY_WINDOW)

        # 步骤 2：检索 + 图片描述
        docs, image_descriptions, image_paths = self.retriever.retrieve(query)

        # 步骤 3：格式化上下文（历史对话 + RAG 检索结果 + 图片描述）
        context = self._format_context(docs, image_descriptions, image_paths, history_text)

        # 步骤 4：LLM 生成回答
        answer = self.chain.invoke({"context": context, "query": query})

        # 步骤 5：保存本轮对话到记忆
        self.memory.save(query, answer, context)

        return answer

    # ============================================================
    # 公开方法：流式问答入口（逐 token 返回）
    # ============================================================
    def ask_stream(self, query: str):
        """
        流式问答入口 —— 逐 token yield，前端可实现打字机效果

        与 ask() 的区别：
            ask()        → 等全部生成后一次性返回字符串
            ask_stream() → 每生成一个 token 就 yield，用户 2 秒内看到第一个字

        内部流程：
            ① 取历史对话 + ② 检索 + ③ 格式化上下文  ← 与 ask() 相同
            ④ self.chain.stream() 逐 token yield    ← 关键区别
            ⑤ 流结束后保存完整回答到记忆

        参数：
            query: 用户自然语言问题

        yield：
            每次返回一小段文字（token），前端拼接显示
        """
        # 步骤 1-3：与 ask() 完全相同
        history_text = self.memory.get_history_text(limit=config.MEMORY_WINDOW)
        docs, image_descriptions, image_paths = self.retriever.retrieve(query)
        context = self._format_context(docs, image_descriptions, image_paths, history_text)

        # 步骤 4：流式生成，逐 token yield
        full_answer = ""
        for chunk in self.chain.stream({"context": context, "query": query}):
            full_answer += chunk
            yield chunk

        # 步骤 5：流结束后保存完整回答
        self.memory.save(query, full_answer, context)


# ============================================================
# 测试入口：命令行交互式问答
# ============================================================
if __name__ == "__main__":
    import sys
    qa = DocxQAService()

    print("=" * 60)
    print(" Docx 文档智能问答")
    print("=" * 60)

    query = input("\n请输入问题: ").strip()
    if not query:
        sys.exit()

    answer = qa.ask(query)
    print(f"\n回答：\n{answer}")
    print("-" * 40)
