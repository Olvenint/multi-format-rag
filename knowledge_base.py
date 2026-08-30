"""
知识库入库服务（通用版，支持所有格式）

- 接收任意 BaseLoader.load() 输出的 Document 列表
- 向量化后存入 Chroma 数据库
- MD5 去重：同一文件内容不变时跳过
- 增量更新：同名文件修改后，先删除旧数据再插入新数据
- 图片描述预计算：入库时一次性调用视觉模型，查询时零 API 调用

与旧版 DocxKnowledgeBase 的区别：
  1. 类名 KnowledgeBase，不再绑定 docx
  2. 支持增量更新（按 source 文件名删除旧数据）
  3. operator 默认值改为空字符串（不硬编码人名）
  4. 测试入口使用 LoaderFactory，支持任意格式
"""
import os
import hashlib                       # 用于文件去重
import datetime                      # 用于记录入库时间
from typing import List
from langchain_chroma import Chroma
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.documents import Document
# 导入配置数据
import config_data as config


# ============================================================
# MD5 工具函数
# ============================================================
def _get_file_md5(file_path: str) -> str:
    """计算文件的 md5 值"""
    md5_obj = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5_obj.update(chunk) 
    return md5_obj.hexdigest()


def _check_md5(md5_str: str) -> bool:
    """检查 md5 是否已处理过"""
    if not os.path.exists(config.MD5_PATH):
        open(config.MD5_PATH, 'w', encoding='utf-8').close()
        return False
    with open(config.MD5_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip() == md5_str:
                return True
    return False


def _save_md5(md5_str: str):
    """记录 md5，标记为已处理"""
    with open(config.MD5_PATH, 'a', encoding='utf-8') as f:
        f.write(md5_str + '\n')


# ============================================================
# 核心类：KnowledgeBase（通用版）
# ============================================================
class KnowledgeBase:
    """
    通用知识库入库服务（支持 docx/pdf/txt/excel 等所有格式）

    用法：
        from loader_factory import LoaderFactory
        from knowledge_base import KnowledgeBase

        loader = LoaderFactory.create("文档.pdf")
        documents = loader.load()

        kb = KnowledgeBase()
        result = kb.upload_documents("文档.pdf", documents)
    """

    def __init__(self):
        os.makedirs(config.PERSIST_DIRECTORY, exist_ok=True)
        self.chroma = Chroma(
            collection_name=config.COLLECTION_NAME,
            embedding_function=DashScopeEmbeddings(model=config.EMBEDDING_MODEL),
            persist_directory=config.PERSIST_DIRECTORY,
        )

    def _delete_by_source(self, source: str) -> int:
        """
        按 source 文件名删除向量库中的旧数据（增量更新核心）。

        当同名文件被修改后重新入库时，先删除旧版本的所有 chunk，
        避免新旧数据混杂。

        参数：
            source: 源文件名（如 "手册.docx"）

        返回：
            删除的记录数
        """
        collection = self.chroma._collection
        try:
            result = collection.get(where={"source": source})
            ids = result.get("ids", [])
            if ids:
                collection.delete(ids=ids)
            return len(ids)
        except Exception as e:
            print(f"  [警告] 删除旧数据失败: {e}")
            return 0

    def upload_documents(
        self,
        file_path: str,
        documents: List[Document],
        operator: str = "",
    ) -> dict:
        """
        将 Loader 已切分好的 Document 列表存入 Chroma。

        增量更新逻辑：
            1. 计算文件 MD5
            2. MD5 已记录 → 文件未变，跳过
            3. MD5 未记录 → 按 source 文件名删除旧数据 → 插入新数据 → 记录 MD5

        参数：
            file_path: 原始文件路径（用于 MD5 去重和 source 识别）
            documents: 任意 BaseLoader.load() 的返回值
            operator: 操作者名称（可选）

        返回：
            {"total": int, "inserted": int, "skipped": int, "deleted": int, "msg": str}
        """
        file_md5 = _get_file_md5(file_path)
        source_name = os.path.basename(file_path)

        # MD5 去重：文件内容没变，直接跳过
        if _check_md5(file_md5):
            return {
                "total": len(documents),
                "inserted": 0,
                "skipped": len(documents),
                "deleted": 0,
                "msg": "该文件内容未变化，已入库，跳过"
            }

        # 增量更新：同名文件有旧数据时先删除
        deleted = self._delete_by_source(source_name)
        if deleted > 0:
            print(f"  增量更新：删除旧版本 {deleted} 条记录")

        inserted = 0

        # ============================================================
        # 预计算图片描述（入库时一次性算好，查询时零 API 调用）
        # ============================================================
        from qa_service import _describe_single_image

        all_image_paths = []
        seen = set()
        for doc in documents:
            images = doc.metadata.get("images", [])
            if isinstance(images, str):
                images = [images]
            for path in images:
                if path and path not in seen:
                    all_image_paths.append(path)
                    seen.add(path)

        image_desc_map = {}
        if all_image_paths:
            print(f"  预计算图片描述：共 {len(all_image_paths)} 张...")
            for i, img_path in enumerate(all_image_paths):
                print(f"    [{i + 1}/{len(all_image_paths)}] {os.path.basename(img_path)}", end="")
                image_desc_map[img_path] = _describe_single_image(img_path)
                print(f" ✓")

        # ============================================================
        # 逐 chunk 入库
        # ============================================================
        for doc in documents:
            text = doc.page_content
            if not text.strip():
                continue

            metadata = dict(doc.metadata)
            metadata.update({
                "create_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "operator": operator,
            })

            # 组装预计算的图片描述
            images = metadata.get("images", [])
            if isinstance(images, str):
                images = [images]
            if images:
                desc_parts = []
                for j, path in enumerate(images):
                    if path in image_desc_map:
                        desc_parts.append(f"图片 {j + 1}：{image_desc_map[path]}")
                metadata["image_description"] = "\n".join(desc_parts) if desc_parts else ""
            else:
                metadata["image_description"] = ""

            # Chroma 不允许 metadata 值为空列表
            metadata = {
                k: v for k, v in metadata.items()
                if not (isinstance(v, list) and len(v) == 0)
            }

            self.chroma.add_texts(
                texts=[text],
                metadatas=[metadata]
            )
            inserted += 1

        # 记录新 MD5
        _save_md5(file_md5)

        return {
            "total": len(documents),
            "inserted": inserted,
            "skipped": len(documents) - inserted,
            "deleted": deleted,
            "msg": "入库成功"
        }


# ============================================================
# 测试入口（支持任意格式）
# ============================================================
if __name__ == "__main__":
    import sys
    from loader_factory import LoaderFactory

    if len(sys.argv) >= 2:
        file_path = sys.argv[1]
    else:
        file_path = config.FILE_PATH

    print(f"文件: {file_path}")

    # 用工厂创建对应格式的 Loader
    loader = LoaderFactory.create(file_path)
    documents = loader.load()
    print(f"解析完成：{len(documents)} 个文档块，{len(loader.extracted_images)} 张图片")

    kb = KnowledgeBase()
    result = kb.upload_documents(file_path, documents)
    print(f"入库结果：{result}")
