"""
BaseLoader — 所有文档解析器的统一抽象基类

设计目的：
    将"文档解析"与"入库/检索"解耦。任何格式（docx、pdf、txt、excel...）
    只要实现 load() 方法返回 List[Document]，就能无缝接入现有入库和问答流程。

子类必须实现：
    load()            -> List[Document]   解析并切分文档
    extracted_images  -> list             提取的图片列表（无图片的格式返回 []）

Document 的 metadata 约定字段：
    source        源文件名（如 "手册.docx"）
    chunk_id      文档块序号（从 1 开始）
    heading       块标题（章节名 / sheet名 / "表格" 等）
    heading_level 标题级别（1=一级标题, 2=二级, 0=表格/特殊）
    chapter       所属一级章节
    doc_type      文档块类型：paragraph / table / sheet
    images        关联图片路径列表
    image_count   关联图片数量
"""
from abc import ABC, abstractmethod     # 声明这是一个抽象基类，真正开启抽象约束
from typing import List
from langchain_core.documents import Document


class BaseLoader(ABC):
    """文档解析器抽象基类"""

    def __init__(self, file_path: str, output_dir: str = None):
        """
        参数：
            file_path:  待解析文档的绝对路径
            output_dir: 图片等提取资源的输出目录，None 时使用默认目录
        """
        self.file_path = file_path
        self.output_dir = output_dir

    @abstractmethod
    def load(self) -> List[Document]:
        """
        解析文档，返回切分好的 LangChain Document 列表。

        返回：
            List[Document]，每个 Document 包含：
                page_content: 文本内容
                metadata:     元数据字典（见模块文档约定）
        """
        pass

    @property
    @abstractmethod
    def extracted_images(self) -> list:
        """
        提取的图片信息列表。
        无图片的格式（如 txt）返回空列表 []。
        每个元素建议为 dict，包含 path、filename 等字段。
        """
        pass
