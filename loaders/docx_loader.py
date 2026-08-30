# ============================================================
# DocxLoader - 基于语义切分的 Word 文档加载器（支持表格）
#
# 核心功能：
#   1. 从 docx 文档中提取所有图片并保存到本地
#   2. 按文档原始顺序遍历段落和表格（doc.element.body）
#   3. 根据标题层级（第一章、1.1、1.2 等）进行语义切分
#   4. 表格转为 Markdown，作为独立 chunk 入库
#   5. 将图片与对应的章节 chunk 关联，保留图文关系
#   6. 输出 LangChain 的 Document 对象，方便后续向量化
#
# 与旧版 DocxSemanticLoader 的关键区别：
#   - 旧版只遍历 doc.paragraphs，表格内容完全丢失
#   - 新版遍历 doc.element.body，保持段落和表格的原始顺序
#   - 新增 _table_to_markdown() 将表格结构化
#   - metadata 新增 doc_type / table_rows / table_cols 字段
# ============================================================
import os
import re
from typing import List, Dict, Any, Optional
from langchain_core.documents import Document
from docx import Document as DocxDocument
from docx.text.paragraph import Paragraph
from docx.table import Table
from docx.oxml.ns import qn
import xml.etree.ElementTree as ET

from loaders.base_loader import BaseLoader

# ============================================================
# 全局常量
# ============================================================
NS_BLIP = '{http://schemas.openxmlformats.org/drawingml/2006/main}blip'
NS_EMBED = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'


class DocxLoader(BaseLoader):
    """
    基于语义切分的 Word 文档加载器（支持表格解析）

    特点：
    - 按标题层级切分文档，保证语义完整
    - 表格转为 Markdown，作为独立 chunk
    - 自动提取并关联图片
    - 输出 LangChain Document 对象
    """

    def __init__(self, file_path: str, output_dir: str = None):
        if output_dir is None:
            import config_data as config
            output_dir = config.OUTPUT_DIR
        super().__init__(file_path, output_dir)
        self.images_dir = os.path.join(output_dir, "images")
        os.makedirs(self.images_dir, exist_ok=True)
        self._extracted_images = []

    # ============================================================
    # 图片提取（与旧版逻辑一致）
    # ============================================================
    def _extract_all_images(self, doc: DocxDocument) -> List[Dict[str, str]]:
        """从 docx 文档中提取所有图片并保存到本地"""
        images = []
        image_counter = 1
        for rel_id, rel in doc.part.rels.items():
            if "image" in rel.target_ref.lower():
                try:
                    image_data = rel.target_part.blob
                    image_ext = rel.target_ref.split(".")[-1].lower()
                    if image_ext not in ["png", "jpg", "jpeg", "gif", "bmp"]:
                        image_ext = "png"
                    image_filename = f"image_{image_counter:04d}.{image_ext}"
                    image_path = os.path.join(self.images_dir, image_filename)
                    with open(image_path, "wb") as f:
                        f.write(image_data)
                    images.append({
                        "rId": rel_id,
                        "filename": image_filename,
                        "path": image_path,
                        "original_name": rel.target_ref
                    })
                    image_counter += 1
                except Exception:
                    pass
        return images

    # ============================================================
    # 标题级别识别（与旧版逻辑一致）
    # ============================================================
    def _detect_heading_level(self, text: str) -> Optional[int]:
        """根据文本内容识别标题级别（正则匹配）"""
        text = text.strip()
        if re.match(r'^第[一二三四五六七八九十]+章', text):
            return 1
        if re.match(r'^\d+\.\d+', text):
            parts = text.split('.')
            if len(parts) == 2 and parts[1][0].isdigit():
                return 2
            elif len(parts) == 3:
                return 3
        if re.match(r'^\d+\s*[、．]', text):
            return 2
        if re.match(r'^\([一二三四五六七八九十\d]+\)', text):
            return 3
        if re.match(r'^[（\(]\d+[）\)]', text):
            return 4
        return None

    # ============================================================
    # 段落图片引用（与旧版逻辑一致）
    # ============================================================
    def _get_images_in_paragraph(self, paragraph: Paragraph) -> List[str]:
        """从单个段落中提取图片的引用 ID（rId）"""
        image_rids = []
        for elem in paragraph._element.iter():
            if elem.tag == NS_BLIP:
                embed = elem.get(NS_EMBED)
                if embed:
                    image_rids.append(embed)
        return image_rids

    def _get_image_paths(self, rids: List[str]) -> List[str]:
        """将图片引用 ID 列表转换为实际文件路径列表"""
        paths = []
        for rid in rids:
            for img in self._extracted_images:
                if img["rId"] == rid and img["path"] not in paths:
                    paths.append(img["path"])
        return paths

    # ============================================================
    # 【新增】按文档原始顺序遍历段落和表格
    # ============================================================
    def _iter_block_items(self, doc: DocxDocument):
        """
        生成器：按文档 body 中的原始顺序，依次产出 Paragraph 和 Table 对象。

        为什么需要这个？
            python-docx 的 doc.paragraphs 和 doc.tables 是两个独立列表，
            互不包含，且都丢失了"段落和表格交错出现"的原始顺序。
            直接遍历 doc.element.body 的 XML 子元素才能还原真实顺序。

         yields:
            Paragraph 或 Table 对象
        """
        for child in doc.element.body:
            if child.tag == qn('w:p'):
                yield Paragraph(child, doc)
            elif child.tag == qn('w:tbl'):
                yield Table(child, doc)
            # 其他元素（如 w:sectPr 分节符）忽略

    # ============================================================
    # 【新增】表格转 Markdown
    # ============================================================
    def _table_to_markdown(self, table: Table) -> str:
        """
        将 docx 表格转为 Markdown 表格文本。

        处理合并单元格：
            python-docx 中，水平/垂直合并的单元格会在 row.cells 中重复出现
            （同一个 _tc 对象被引用多次）。通过 id(cell._tc) 去重，
            保证同一行中合并的单元格只输出一次。

        参数：
            table: python-docx 的 Table 对象

        返回：
            Markdown 表格字符串；空表格返回空字符串
        """
        rows = []
        for row in table.rows:
            seen_tc = set()
            cells = []
            for cell in row.cells:
                tc_id = id(cell._tc)
                if tc_id not in seen_tc:
                    seen_tc.add(tc_id)
                    # 单元格内可能有多段文字，用空格连接，去除换行
                    text = cell.text.strip().replace('\n', ' ').replace('\r', '')
                    cells.append(text)
            rows.append(cells)

        if not rows or not any(rows):
            return ""

        # 以最大列数为准，不足的补空字符串
        col_count = max(len(r) for r in rows)
        for r in rows:
            while len(r) < col_count:
                r.append("")

        # 第一行作为表头
        lines = []
        lines.append("| " + " | ".join(rows[0]) + " |")
        lines.append("| " + " | ".join(["---"] * col_count) + " |")
        for r in rows[1:]:
            lines.append("| " + " | ".join(r) + " |")

        return "\n".join(lines)

    # ============================================================
    # 核心方法：加载并切分文档（重写遍历逻辑）
    # ============================================================
    def load(self) -> List[Document]:
        """
        加载 docx 文档，按语义（标题）切分，表格转为独立 chunk，并关联图片。

        与旧版的关键区别：
            旧版：for paragraph in doc.paragraphs  →  只处理段落，表格丢失
            新版：for block in self._iter_block_items(doc)  →  段落+表格按顺序处理

        返回：
            List[Document]，每个 Document 包含 page_content 和 metadata
        """
        doc = DocxDocument(self.file_path)
        self._extracted_images = self._extract_all_images(doc)

        chunks = []
        current_chunk = None
        current_chapter = ""
        chunk_id = 1

        # 【关键改动】按文档原始顺序遍历段落和表格
        for block in self._iter_block_items(doc):

            # ---------- 情况 A：当前 block 是段落 ----------
            if isinstance(block, Paragraph):
                text = block.text.strip()
                paragraph_images = self._get_images_in_paragraph(block)

                # 跳过空段落（既没有文本也没有图片）
                if not text and not paragraph_images:
                    continue

                heading_level = self._detect_heading_level(text) if text else None

                # 一级标题：更新当前章节，不单独成 chunk
                if heading_level == 1:
                    current_chapter = text
                    continue

                # 二级标题：先保存当前 chunk，再开启新 chunk
                if heading_level == 2:
                    if current_chunk:
                        chunks.append(self._create_document(current_chunk, chunk_id))
                        chunk_id += 1
                    current_chunk = {
                        "text": "",
                        "heading": text,
                        "heading_level": 2,
                        "chapter": current_chapter,
                        "image_rids": paragraph_images.copy(),
                        "is_table": False,
                    }
                    continue

                # 普通正文段落：追加到当前 chunk
                if current_chunk:
                    if text:
                        current_chunk["text"] += text + "\n\n"
                    if paragraph_images:
                        current_chunk["image_rids"].extend(paragraph_images)
                else:
                    # 文档开头没有标题的情况
                    current_chunk = {
                        "text": text + "\n\n" if text else "",
                        "heading": current_chapter or "文档开头",
                        "heading_level": 1,
                        "chapter": current_chapter,
                        "image_rids": paragraph_images.copy(),
                        "is_table": False,
                    }

            # ---------- 情况 B：当前 block 是表格 ----------
            elif isinstance(block, Table):
                table_md = self._table_to_markdown(block)
                if not table_md.strip():
                    continue

                # 先保存当前正在构建的 chunk（如果有）
                if current_chunk:
                    chunks.append(self._create_document(current_chunk, chunk_id))
                    chunk_id += 1

                # 表格作为独立 chunk
                table_heading = f"{current_chapter} - 表格" if current_chapter else "表格"
                current_chunk = {
                    "text": table_md,
                    "heading": table_heading,
                    "heading_level": 0,          # 0 标识表格类型
                    "chapter": current_chapter,
                    "image_rids": [],
                    "is_table": True,
                    "table_rows": len(block.rows),
                    "table_cols": len(block.columns),
                }

        # 保存最后一个 chunk
        if current_chunk:
            chunks.append(self._create_document(current_chunk, chunk_id))

        return chunks

    # ============================================================
    # 创建 LangChain Document 对象（扩展 metadata）
    # ============================================================
    def _create_document(self, chunk: Dict[str, Any], chunk_id: int) -> Document:
        """将 chunk 字典转换为 LangChain Document 对象"""
        image_paths = self._get_image_paths(chunk["image_rids"])

        is_table = chunk.get("is_table", False)
        metadata = {
            "source": os.path.basename(self.file_path),
            "chunk_id": chunk_id,
            "heading": chunk["heading"],
            "heading_level": chunk["heading_level"],
            "chapter": chunk.get("chapter", ""),
            "images": image_paths,
            "image_count": len(image_paths),
            "total_images": len(self._extracted_images),
            # 【新增字段】
            "doc_type": "table" if is_table else "paragraph",
            "table_rows": chunk.get("table_rows", 0),
            "table_cols": chunk.get("table_cols", 0),
        }

        return Document(
            page_content=chunk["text"].strip(),
            metadata=metadata
        )

    # ============================================================
    # BaseLoader 接口实现
    # ============================================================
    @property
    def extracted_images(self) -> list:
        """提取的图片信息列表"""
        return self._extracted_images


# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python docx_loader.py <docx_file_path>")
        sys.exit(1)

    loader = DocxLoader(sys.argv[1])
    documents = loader.load()

    print(f"共生成 {len(documents)} 个文档块")
    print(f"共提取 {len(loader.extracted_images)} 张图片")
    print("=" * 60)

    table_count = 0
    for i, doc in enumerate(documents):
        doc_type = doc.metadata.get("doc_type", "paragraph")
        if doc_type == "table":
            table_count += 1
        print(f"\n--- Chunk {i+1} [{doc_type}] ---")
        print(f"章节: {doc.metadata.get('chapter', '无')}")
        print(f"标题: {doc.metadata.get('heading', '无标题')}")
        print(f"标题级别: {doc.metadata.get('heading_level', '正文')}")
        if doc_type == "table":
            print(f"表格: {doc.metadata.get('table_rows', 0)} 行 x {doc.metadata.get('table_cols', 0)} 列")
        print(f"图片数量: {doc.metadata.get('image_count', 0)} / {doc.metadata.get('total_images', 0)}")
        print(f"内容长度: {len(doc.page_content)} 字符")
        preview = doc.page_content[:200].replace('\n', ' ')
        print(f"内容预览: {preview}...")
        print("-" * 40)

    print(f"\n统计：共 {len(documents)} 个块，其中表格块 {table_count} 个")
