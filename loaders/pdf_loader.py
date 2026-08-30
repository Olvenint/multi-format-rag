# ============================================================
# PdfLoader - PDF 文档加载器
#
# 核心功能：
#   1. 用 PyMuPDF (fitz) 提取每页文本和图片
#   2. 用 pdfplumber 提取表格（可选，未安装时跳过）
#   3. 按页 + 标题正则进行语义切分
#   4. 表格转为 Markdown，作为独立 chunk
#   5. 图片保存到本地并与所在页 chunk 关联
#
# 依赖：
#   pip install pymupdf pdfplumber
#   - pymupdf: 文本和图片提取（必需）
#   - pdfplumber: 表格提取（可选，未安装时表格跳过）
# ============================================================
import os
import re
from typing import List, Dict, Any, Optional
from langchain_core.documents import Document

from loaders.base_loader import BaseLoader


class PdfLoader(BaseLoader):
    """
    PDF 文档加载器

    特点：
    - PyMuPDF 提取文本和图片，速度快、兼容性好
    - pdfplumber 提取表格（可选），转 Markdown
    - 按页切分，页内按标题正则进一步细分
    - 图片与所在页 chunk 关联
    """

    def __init__(self, file_path: str, output_dir: str = None):
        if output_dir is None:
            import config_data as config
            output_dir = config.OUTPUT_DIR
        super().__init__(file_path, output_dir)
        self.images_dir = os.path.join(output_dir, "images")
        os.makedirs(self.images_dir, exist_ok=True)
        self._extracted_images = []
        self._image_counter = 1

    # ============================================================
    # 标题识别（复用 docx 的规则，适配 PDF 文本）
    # ============================================================
    def _detect_heading_level(self, text: str) -> Optional[int]:
        """根据文本内容识别标题级别"""
        text = text.strip()
        if not text or len(text) > 100:  # 标题不会太长
            return None
        if re.match(r'^第[一二三四五六七八九十]+[章节]', text):
            return 1
        if re.match(r'^\d+\.\d+', text):
            parts = text.split('.')
            if len(parts) == 2 and parts[1] and parts[1][0].isdigit():
                return 2
            elif len(parts) >= 3:
                return 3
        if re.match(r'^\d+\s*[、．.]\s*\S', text):
            return 2
        if re.match(r'^\([一二三四五六七八九十\d]+\)', text):
            return 3
        return None

    # ============================================================
    # 提取单页图片（PyMuPDF）
    # ============================================================
    def _extract_images_from_page(self, page, page_num: int) -> List[str]:
        """
        从 PDF 单页提取图片，保存到本地，返回图片路径列表。

        参数：
            page: fitz.Page 对象
            page_num: 页码（从 1 开始）

        返回：
            保存后的图片绝对路径列表
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return []

        image_paths = []
        try:
            image_list = page.get_images(full=True)
            for img_index, img in enumerate(image_list):
                xref = img[0]
                base_image = page.parent.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image.get("ext", "png")

                filename = f"pdf_p{page_num:03d}_{img_index + 1:02d}.{image_ext}"
                image_path = os.path.join(self.images_dir, filename)

                with open(image_path, "wb") as f:
                    f.write(image_bytes)

                image_paths.append(image_path)
                self._extracted_images.append({
                    "rId": f"pdf_p{page_num}_{img_index}",
                    "filename": filename,
                    "path": image_path,
                    "page": page_num,
                })
        except Exception:
            pass

        return image_paths

    # ============================================================
    # 提取单页表格（pdfplumber，可选）
    # ============================================================
    def _extract_tables_from_page(self, pdfplumber_page) -> List[str]:
        """
        用 pdfplumber 提取单页表格，转为 Markdown 列表。

        参数：
            pdfplumber_page: pdfplumber.Page 对象

        返回：
            Markdown 表格字符串列表
        """
        tables_md = []
        try:
            tables = pdfplumber_page.extract_tables()
        except Exception:
            return tables_md

        for table in tables:
            if not table or not table[0]:
                continue
            # 清理单元格：None → ""，去除换行
            cleaned = []
            for row in table:
                cleaned_row = [(cell or "").strip().replace("\n", " ") for cell in row]
                cleaned.append(cleaned_row)

            col_count = max(len(r) for r in cleaned)
            for r in cleaned:
                while len(r) < col_count:
                    r.append("")

            lines = []
            lines.append("| " + " | ".join(cleaned[0]) + " |")
            lines.append("| " + " | ".join(["---"] * col_count) + " |")
            for r in cleaned[1:]:
                lines.append("| " + " | ".join(r) + " |")
            tables_md.append("\n".join(lines))

        return tables_md

    # ============================================================
    # 页内文本按标题切分
    # ============================================================
    def _split_page_text(self, text: str, page_num: int,
                         image_paths: List[str]) -> List[Dict[str, Any]]:
        """
        将单页文本按标题正则切分为多个 chunk 字典。

        返回：
            chunk 字典列表，每个包含 text、heading、heading_level、image_rids 等
        """
        chunks = []
        current_chunk = None
        current_chapter = f"第{page_num}页"

        # 按行处理
        lines = text.split("\n")
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                if current_chunk:
                    current_chunk["text"] += "\n"
                continue

            heading_level = self._detect_heading_level(line_stripped)

            if heading_level == 1:
                # 保存当前 chunk
                if current_chunk:
                    chunks.append(current_chunk)
                current_chapter = line_stripped
                current_chunk = {
                    "text": "",
                    "heading": line_stripped,
                    "heading_level": 1,
                    "chapter": current_chapter,
                    "images": image_paths,
                    "is_table": False,
                }
            elif heading_level == 2:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = {
                    "text": "",
                    "heading": line_stripped,
                    "heading_level": 2,
                    "chapter": current_chapter,
                    "images": image_paths,
                    "is_table": False,
                }
            else:
                if current_chunk:
                    current_chunk["text"] += line_stripped + "\n"
                else:
                    current_chunk = {
                        "text": line_stripped + "\n",
                        "heading": current_chapter,
                        "heading_level": 1,
                        "chapter": current_chapter,
                        "images": image_paths,
                        "is_table": False,
                    }

        if current_chunk:
            chunks.append(current_chunk)

        # 如果整页没有识别出任何内容，创建一个默认 chunk
        if not chunks and text.strip():
            chunks.append({
                "text": text.strip(),
                "heading": f"第{page_num}页",
                "heading_level": 1,
                "chapter": f"第{page_num}页",
                "images": image_paths,
                "is_table": False,
            })

        return chunks

    # ============================================================
    # 核心方法：加载并切分
    # ============================================================
    def load(self) -> List[Document]:
        """
        加载 PDF 文档，逐页提取文本、图片、表格，按标题切分。

        返回：
            List[Document]
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ImportError(
                "PDF 解析需要安装 PyMuPDF：pip install pymupdf\n"
                "表格提取可选安装：pip install pdfplumber"
            )

        # 可选：pdfplumber 用于表格提取
        try:
            import pdfplumber
            has_pdfplumber = True
        except ImportError:
            has_pdfplumber = False
            print("  [提示] 未安装 pdfplumber，将跳过 PDF 表格提取。安装：pip install pdfplumber")

        doc = fitz.open(self.file_path)
        all_chunks = []
        chunk_id = 1

        # pdfplumber 打开一次，用于表格提取
        pdfplumber_doc = None
        if has_pdfplumber:
            try:
                pdfplumber_doc = pdfplumber.open(self.file_path)
            except Exception:
                pdfplumber_doc = None

        for page_idx in range(len(doc)):
            page_num = page_idx + 1
            page = doc[page_idx]

            # 1. 提取文本
            text = page.get_text("text")

            # 2. 提取图片
            image_paths = self._extract_images_from_page(page, page_num)

            # 3. 提取表格（pdfplumber）
            table_md_list = []
            if pdfplumber_doc and page_idx < len(pdfplumber_doc.pages):
                try:
                    table_md_list = self._extract_tables_from_page(
                        pdfplumber_doc.pages[page_idx]
                    )
                except Exception:
                    pass

            # 4. 页内文本按标题切分
            page_chunks = self._split_page_text(text, page_num, image_paths)
            for chunk in page_chunks:
                if chunk["text"].strip():
                    all_chunks.append(self._create_document(chunk, chunk_id))
                    chunk_id += 1

            # 5. 表格作为独立 chunk
            for table_md in table_md_list:
                table_chunk = {
                    "text": table_md,
                    "heading": f"第{page_num}页 - 表格",
                    "heading_level": 0,
                    "chapter": f"第{page_num}页",
                    "images": [],
                    "is_table": True,
                    "table_rows": table_md.count("\n") - 1,  # 粗略估算
                    "table_cols": table_md.count("|") // 2 - 1,
                }
                all_chunks.append(self._create_document(table_chunk, chunk_id))
                chunk_id += 1

        doc.close()
        if pdfplumber_doc:
            pdfplumber_doc.close()

        return all_chunks

    # ============================================================
    # 创建 Document
    # ============================================================
    def _create_document(self, chunk: Dict[str, Any], chunk_id: int) -> Document:
        """创建 LangChain Document 对象"""
        is_table = chunk.get("is_table", False)
        images = chunk.get("images", [])
        metadata = {
            "source": os.path.basename(self.file_path),
            "chunk_id": chunk_id,
            "heading": chunk["heading"],
            "heading_level": chunk["heading_level"],
            "chapter": chunk.get("chapter", ""),
            "doc_type": "table" if is_table else "paragraph",
            "images": images,
            "image_count": len(images),
            "total_images": len(self._extracted_images),
            "table_rows": chunk.get("table_rows", 0),
            "table_cols": chunk.get("table_cols", 0),
        }
        return Document(page_content=chunk["text"].strip(), metadata=metadata)

    # ============================================================
    # BaseLoader 接口
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
        print("Usage: python pdf_loader.py <pdf_file_path>")
        sys.exit(1)

    loader = PdfLoader(sys.argv[1])
    documents = loader.load()
    print(f"共生成 {len(documents)} 个文档块")
    print(f"共提取 {len(loader.extracted_images)} 张图片")
    print("=" * 60)
    table_count = sum(1 for d in documents if d.metadata.get("doc_type") == "table")
    for i, doc in enumerate(documents):
        doc_type = doc.metadata.get("doc_type", "paragraph")
        print(f"\n--- Chunk {i+1} [{doc_type}] ---")
        print(f"标题: {doc.metadata.get('heading', '无')}")
        print(f"内容长度: {len(doc.page_content)} 字符")
        preview = doc.page_content[:150].replace('\n', ' ')
        print(f"预览: {preview}...")
    print(f"\n统计：{len(documents)} 块（含 {table_count} 个表格块），{len(loader.extracted_images)} 张图片")
