# ============================================================
# ExcelLoader - Excel 文档加载器
#
# 核心功能：
#   1. 用 openpyxl 读取 .xlsx 文件，遍历所有 sheet
#   2. 每个 sheet 的已用区域转为 Markdown 表格
#   3. 小 sheet（<=50行）整个作为一个 chunk
#   4. 大 sheet 按行块切分（每 50 行一块，保留表头）
#   5. metadata 记录 sheet_name、row_count、col_count
#
# 依赖：
#   pip install openpyxl
#
# 注意：
#   .xls 旧格式需要 xlrd 库，本 Loader 主要支持 .xlsx。
#   如遇 .xls 文件，建议先用 Excel 另存为 .xlsx。
# ============================================================
import os
from typing import List, Dict, Any
from langchain_core.documents import Document

from loaders.base_loader import BaseLoader

# 大 sheet 切分参数
_MAX_ROWS_PER_CHUNK = 50   # 每个 chunk 最多多少行数据（不含表头）


class ExcelLoader(BaseLoader):
    """
    Excel 文档加载器

    特点：
    - 遍历所有 sheet，每个 sheet 独立处理
    - 已用区域自动识别，跳过空行空列
    - 第一行作为表头，Markdown 表格输出
    - 大 sheet 按行块切分，每块保留表头
    - 无图片，extracted_images 返回空列表
    """

    def __init__(self, file_path: str, output_dir: str = None):
        super().__init__(file_path, output_dir)
        self._extracted_images = []

    # ============================================================
    # 读取 sheet 数据
    # ============================================================
    def _read_sheet(self, worksheet) -> List[List[str]]:
        """
        读取一个 sheet 的所有数据，返回二维列表。

        自动跳过完全为空的行和列。
        单元格值转为字符串，None 转为空字符串。
        """
        rows = []
        for row in worksheet.iter_rows(values_only=True):
            # 将每个单元格转为字符串
            str_row = ["" if cell is None else str(cell).strip() for cell in row]
            # 跳过全空行
            if any(str_row):
                rows.append(str_row)

        if not rows:
            return []

        # 去除尾部全空列
        max_col = max(len(r) for r in rows)
        # 从右往左找第一列非空
        last_non_empty_col = 0
        for col_idx in range(max_col - 1, -1, -1):
            if any(row[col_idx] if col_idx < len(row) else "" for row in rows):
                last_non_empty_col = col_idx
                break

        # 截断到最后一个非空列
        trimmed = []
        for r in rows:
            trimmed.append(r[:last_non_empty_col + 1])

        return trimmed

    # ============================================================
    # 二维列表转 Markdown 表格
    # ============================================================
    def _rows_to_markdown(self, rows: List[List[str]], header: List[str] = None) -> str:
        """
        将二维列表转为 Markdown 表格。

        参数：
            rows:   数据行（不含表头）
            header: 表头行；为 None 时取 rows[0] 作为表头
        """
        if not rows:
            return ""

        if header is None:
            header = rows[0]
            data_rows = rows[1:]
        else:
            data_rows = rows

        if not header:
            return ""

        col_count = len(header)
        # 补齐数据行列数
        for r in data_rows:
            while len(r) < col_count:
                r.append("")

        lines = []
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * col_count) + " |")
        for r in data_rows:
            lines.append("| " + " | ".join(r[:col_count]) + " |")

        return "\n".join(lines)

    # ============================================================
    # 处理单个 sheet
    # ============================================================
    def _process_sheet(self, sheet_name: str, worksheet,
                       chunk_id_start: int) -> tuple:
        """
        处理单个 sheet，返回 (Document 列表, 下一个 chunk_id)。

        小 sheet（<= _MAX_ROWS_PER_CHUNK 行数据）：整个 sheet 一个 chunk
        大 sheet：按行块切分，每块保留表头
        """
        rows = self._read_sheet(worksheet)
        if not rows:
            return [], chunk_id_start

        header = rows[0]
        data_rows = rows[1:]
        documents = []
        chunk_id = chunk_id_start

        if len(data_rows) <= _MAX_ROWS_PER_CHUNK:
            # 小 sheet：整个作为一个 chunk
            md = self._rows_to_markdown(rows)
            doc = self._create_document(
                text=md,
                chunk_id=chunk_id,
                sheet_name=sheet_name,
                row_count=len(rows),
                col_count=len(header),
                part_info=f"全部 {len(data_rows)} 行",
            )
            documents.append(doc)
            chunk_id += 1
        else:
            # 大 sheet：按行块切分，每块保留表头
            total_parts = (len(data_rows) + _MAX_ROWS_PER_CHUNK - 1) // _MAX_ROWS_PER_CHUNK
            for part_idx in range(total_parts):
                start = part_idx * _MAX_ROWS_PER_CHUNK
                end = min(start + _MAX_ROWS_PER_CHUNK, len(data_rows))
                part_rows = [header] + data_rows[start:end]
                md = self._rows_to_markdown(part_rows, header=header)

                doc = self._create_document(
                    text=md,
                    chunk_id=chunk_id,
                    sheet_name=sheet_name,
                    row_count=len(rows),
                    col_count=len(header),
                    part_info=f"第 {part_idx + 1}/{total_parts} 部分（行 {start + 1}-{end}）",
                )
                documents.append(doc)
                chunk_id += 1

        return documents, chunk_id

    # ============================================================
    # 核心方法：加载并切分
    # ============================================================
    def load(self) -> List[Document]:
        """
        加载 Excel 文件，遍历所有 sheet，转为 Markdown 表格 chunk。

        返回：
            List[Document]
        """
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise ImportError(
                "Excel 解析需要安装 openpyxl：pip install openpyxl"
            )

        # .xls 旧格式检测
        ext = os.path.splitext(self.file_path)[1].lower()
        if ext == ".xls":
            print("  [提示] .xls 旧格式支持有限，建议先用 Excel 另存为 .xlsx")

        wb = load_workbook(filename=self.file_path, read_only=True, data_only=True)

        all_documents = []
        chunk_id = 1

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            docs, chunk_id = self._process_sheet(sheet_name, ws, chunk_id)
            all_documents.extend(docs)

        wb.close()
        return all_documents

    # ============================================================
    # 创建 Document
    # ============================================================
    def _create_document(self, text: str, chunk_id: int, sheet_name: str,
                         row_count: int, col_count: int,
                         part_info: str = "") -> Document:
        """创建 LangChain Document 对象"""
        heading = f"Sheet: {sheet_name}"
        if part_info:
            heading += f"（{part_info}）"

        metadata = {
            "source": os.path.basename(self.file_path),
            "chunk_id": chunk_id,
            "heading": heading,
            "heading_level": 0,            # 0 标识表格/数据类型
            "chapter": sheet_name,
            "doc_type": "sheet",           # Excel sheet 类型
            "sheet_name": sheet_name,
            "row_count": row_count,
            "col_count": col_count,
            "images": [],
            "image_count": 0,
            "total_images": 0,
            "table_rows": row_count,
            "table_cols": col_count,
        }
        return Document(page_content=text.strip(), metadata=metadata)

    # ============================================================
    # BaseLoader 接口
    # ============================================================
    @property
    def extracted_images(self) -> list:
        """Excel 无图片提取，返回空列表"""
        return self._extracted_images


# ============================================================
# 命令行测试入口
# ============================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python excel_loader.py <excel_file_path>")
        sys.exit(1)

    loader = ExcelLoader(sys.argv[1])
    documents = loader.load()
    print(f"共生成 {len(documents)} 个文档块")
    print("=" * 60)
    for i, doc in enumerate(documents):
        print(f"\n--- Chunk {i+1} [{doc.metadata.get('doc_type')}] ---")
        print(f"标题: {doc.metadata.get('heading', '无')}")
        print(f"Sheet: {doc.metadata.get('sheet_name', '无')}")
        print(f"行列: {doc.metadata.get('row_count', 0)} 行 x {doc.metadata.get('col_count', 0)} 列")
        print(f"内容长度: {len(doc.page_content)} 字符")
        preview = doc.page_content[:200].replace('\n', ' ')
        print(f"预览: {preview}...")
