"""
LoaderFactory — 根据文件扩展名选择对应的文档解析器

使用方式：
    from loader_factory import LoaderFactory
    loader = LoaderFactory.create("文档.pdf")
    documents = loader.load()

设计要点：
    1. 延迟导入：只在 create() 时才 import 对应 Loader，避免某格式依赖缺失导致整个工厂不可用。
    2. 注册表驱动：新增格式只需在 _registry 中加一行，无需修改工厂逻辑。
    3. 统一接口：所有 Loader 继承 BaseLoader，返回 List[Document]。

总结：
    传入一个文件的绝对路径，如果文件扩展名在注册表中，说明我们能解析这种格式的文件 ——> 工厂会根据文件扩展名选择对应的 Loader 实例
    如果文件扩展名不在注册表中，说明我们不能解析这种格式的文件 ——> 工厂会抛出 ValueError 异常,并且提示不支持的文件格式和当前支持解析的格式。
"""
import os
from typing import Type
from loaders.base_loader import BaseLoader


class LoaderFactory:
    """文档解析器工厂 — 按文件扩展名分发到对应 Loader"""

    # 扩展名 -> Loader 类的全路径（模块名.类名），延迟导入
    _registry = {
        ".docx": "loaders.docx_loader.DocxLoader",
        ".pdf":  "loaders.pdf_loader.PdfLoader",
        ".txt":  "loaders.txt_loader.TxtLoader",
        ".md":   "loaders.txt_loader.TxtLoader",   # markdown 按纯文本处理
        ".xlsx": "loaders.excel_loader.ExcelLoader",
        ".xls":  "loaders.excel_loader.ExcelLoader",
    }

    @classmethod
    def create(cls, file_path: str, output_dir: str = None) -> BaseLoader:
        """
        根据文件扩展名创建对应的 Loader 实例。

        参数：
            file_path:  文档绝对路径
            output_dir: 图片等资源输出目录，None 时由 Loader 自行决定默认目录

        返回：
            BaseLoader 子类实例

        抛出：
            ValueError: 不支持的文件格式
            ImportError: 对应 Loader 的依赖未安装（如 PDF 需要 pymupdf）
        """
        ext = os.path.splitext(file_path)[1].lower()  # 把文件路径拆成文件名和扩展名，之后取小写扩展名
        class_path = cls._registry.get(ext)  # 从注册表中获取对应的 Loader 类的全路径

        # 抛出 ValueError 异常，提示不支持的文件格式和当前支持的格式
        if not class_path:
            supported = ", ".join(sorted(cls._registry.keys()))
            raise ValueError(
                f"不支持的文件格式: '{ext}'\n"
                f"当前支持格式: {supported}"
            )     

        # 延迟导入：从 "loaders.pdf_loader.PdfLoader" 动态导入
        module_path, class_name = class_path.rsplit(".", 1)   # 把类路径拆成模块路径和类名
        import importlib      # 不写死导入所有模块，动态导入需要的模块，避免依赖缺失
        module = importlib.import_module(module_path)  
        loader_cls = getattr(module, class_name)  # 从模块中获取对应的类

        if output_dir:
            return loader_cls(file_path, output_dir=output_dir)
        return loader_cls(file_path)

    @classmethod
    def supported_extensions(cls) -> list:
        """返回所有支持的文件扩展名列表"""
        return sorted(cls._registry.keys())

    @classmethod
    def is_supported(cls, file_path: str) -> bool:
        """判断给定文件是否为支持的格式"""
        ext = os.path.splitext(file_path)[1].lower()  # 把文件路径拆成文件名和扩展名，之后取小写扩展名
        return ext in cls._registry
