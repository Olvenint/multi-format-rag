"""
loaders 包 — 多格式文档解析器

每个 Loader 负责将一种格式的文档解析为 LangChain Document 列表，
统一接口由 base_loader.BaseLoader 定义，通过 loader_factory.LoaderFactory 按扩展名分发。
"""
from loaders.base_loader import BaseLoader

__all__ = ["BaseLoader"]
