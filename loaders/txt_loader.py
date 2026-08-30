# ============================================================
# TxtLoader - 纯文本 / Markdown 文档加载器
#
# 核心功能：
#   1. 读取 .txt / .md 文件（自动尝试 utf-8 / gbk 编码）
#   2. 按空行分段（段落级语义切分）
#   3. 超长段落用递归字符分割（800字 + 100字重叠）
#   4. 输出 LangChain Document 对象
#
# 适用场景：纯文本文档、Markdown 笔记、日志文件、代码说明等
# ============================================================
import os
import re
from typing import List
from langchain_core.documents import Document

from loaders.base_loader import BaseLoader

# 递归分割参数
_CHUNK_SIZE = 800       # 单块最大字符数
_CHUNK_OVERLAP = 100    # 相邻块重叠字符数


class TxtLoader(BaseLoader):
    """
    纯文本文档加载器

    特点：
    - 自动识别编码（utf-8 → gbk → latin-1 回退）
    - 按空行分段，保留段落语义
    - 超长段落递归分割，带重叠窗口
    - 无图片，extracted_images 返回空列表
    """

    def __init__(self, file_path: str, output_dir: str = None):
        super().__init__(file_path, output_dir)
        self._extracted_images = []

    # ============================================================
    # 读取文件（自动编码识别）
    # ============================================================
    def _read_text(self) -> str:
        """读取文本文件，自动尝试多种编码"""
        encodings = ["utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"]
        for enc in encodings:
            try:
                with open(self.file_path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue

        # errors参数：遇到无法解码的字节（乱码、非法字节）时，决定 Python 怎么处理错误
        # 默认为"strict",即抛出异常
        # errors="replace"：遇到无法解码的字节，用替换字符（如"?""）代替
        # errors="ignore"：遇到无法解码的字节，直接忽略过,不输出任何占位符号
        with open(self.file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    # ============================================================
    # 按空行分段
    # ============================================================
    def _split_by_paragraph(self, text: str) -> List[str]:
        """
        按空行将文本分割为段落列表。

        支持 \n\n、\r\n\r\n、多个连续空行等情况。
        去除每段首尾空白，过滤空段落。
        """
        # 统一换行符
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # 按一个或多个空行分割
        paragraphs = re.split(r'\n\s*\n', text)  
        # 去除每段首尾空白，过滤空段
        return [p.strip() for p in paragraphs if p.strip()]

    # ============================================================
    # 循环字符分割（处理超长段落）
    # ============================================================
    def _sliding_split(self, text: str, chunk_size: int = _CHUNK_SIZE,
                         overlap: int = _CHUNK_OVERLAP) -> List[str]:
        """
        对超长段落做循环字符分割。

        优先在句号、问号、感叹号等句子边界切分，
        其次在逗号、分号，最后在任意字符切分。
        相邻块保留 overlap 字符的重叠，避免语义断裂。

        不依赖 LangChain 的 RecursiveCharacterTextSplitter，
        纯标准库实现，减少外部依赖。
        """
        if len(text) <= chunk_size:
            return [text]

        # 分割优先级：句子结束符 > 分句符 > 空格 > 任意位置
        separators = ["\n", "。", "！", "？", "；", ".", "!", "?", ";", "，", ",", " ", ""]

        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))

            # 如果不是最后一段，尝试在 separators 中找到合适的切点
            if end < len(text):
                cut_pos = -1
                for sep in separators:
                    if sep == "":
                        cut_pos = end  # 强制切
                        break
                    # 在 [end - overlap, end] 范围内找最后一个分隔符
                    search_start = max(start + 1, end - overlap)
                    pos = text.rfind(sep, search_start, end) # 从后往前找，找到第一个遇见的 sep 的索引，如果找不到返回 -1
                    if pos != -1:  # 找到分隔符
                        cut_pos = pos + len(sep)  
                        break

                if cut_pos > start:
                    end = cut_pos

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            # 下一块起点，保留重叠
            start = end
            if start < len(text) and overlap > 0:
                start = max(start - overlap, 0)
                # 确保前进至少 1 字符，防止死循环
                if start >= end:
                    start = end

        return chunks

    # ============================================================
    # 核心方法：加载并切分
    # ============================================================
    def load(self) -> List[Document]:
        """
        加载纯文本文档，按段落 + 递归分割切分。

        返回：
            List[Document]
        """
        raw_text = self._read_text()
        paragraphs = self._split_by_paragraph(raw_text)

        documents = []
        chunk_id = 1
        current_heading = os.path.splitext(os.path.basename(self.file_path))[0]

        for para in paragraphs:
            # Markdown 标题识别（# 开头），作为后续段落的 heading
            heading_match = re.match(r'^(#{1,6})\s*(.+)', para)
            if heading_match:
                hash_part = heading_match.group(1)
                current_heading = heading_match.group(2).strip()
                real_level = len(hash_part)
                documents.append(self._create_document(para, chunk_id, current_heading, real_level))
                chunk_id += 1
                continue


            # 普通段落：如果超长，递归分割
            if len(para) > _CHUNK_SIZE:
                sub_chunks = self._sliding_split(para)
                for sub in sub_chunks:
                    documents.append(self._create_document(sub, chunk_id, current_heading, 2))
                    chunk_id += 1
            else:
                documents.append(self._create_document(para, chunk_id, current_heading, 2))
                chunk_id += 1

        # 如果文档为空，至少返回一个空 Document
        if not documents:
            documents.append(self._create_document("", 1, current_heading, 2))

        return documents

    # ============================================================
    # 创建 Document
    # ============================================================
    def _create_document(self, text: str, chunk_id: int, heading: str,
                         heading_level: int) -> Document:
        """创建 LangChain Document 对象"""
        metadata = {
            "source": os.path.basename(self.file_path),
            "chunk_id": chunk_id,
            "heading": heading,
            "heading_level": heading_level,
            "chapter": "",
            "doc_type": "paragraph",
            "images": [],
            "image_count": 0,
            "total_images": 0,
            "table_rows": 0,
            "table_cols": 0,
        }
        return Document(page_content=text.strip(), metadata=metadata)

    # ============================================================
    # BaseLoader 接口
    # ============================================================
    @property
    def extracted_images(self) -> list:
        """纯文本无图片，返回空列表"""
        return self._extracted_images


# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python txt_loader.py <txt_file_path>")
        sys.exit(1)

    loader = TxtLoader(sys.argv[1])
    documents = loader.load()
    print(f"共生成 {len(documents)} 个文档块")
    print("=" * 60)
    for i, doc in enumerate(documents):
        print(f"\n--- Chunk {i+1} ---")
        print(f"标题: {doc.metadata.get('heading', '无')}")
        print(f"内容长度: {len(doc.page_content)} 字符")
        preview = doc.page_content[:150].replace('\n', ' ')
        print(f"预览: {preview}...")
