# Multi-Format RAG System

一个**本地、多格式**的知识库问答（RAG）系统：把 `docx / pdf / txt / excel` 文档解析、向量化、存入本地向量库，再基于检索结果调用大模型回答你的问题。

> 本项目为个人学习项目（v2.0），支持离线上库 + 在线问答两条流程，所有数据存本地，无需部署服务。

## 功能特性

- **多格式解析**：docx（含表格）、PDF、TXT、Excel 四类文档统一入库
- **文件级去重**：基于文件 MD5，重复文件自动跳过
- **本地向量库**：Chroma 持久化存储，重启不丢
- **表格感知**：docx 表格按行拆分为独立文档片段，保留结构语义
- **PDF 图文并重**：文本用 pdfplumber 抽取、含图页面用视觉模型转写
- **对话记忆**：滑动窗口记录最近 N 轮对话，支持多轮追问
- **Web 界面**：基于 Gradio，开箱即用

## 系统流程

```
用户文档(docx/pdf/txt/excel)
        │  Loader 解析（工厂模式 + 抽象基类）
        ▼
  结构化文本片段 + 元数据
        │  嵌入模型(text-embedding-v4)
        ▼
   Chroma 向量库（本地持久化）
        │  检索 TOP-K
        ▼
  用户问题 ──► 相似片段 ──► 大模型(deepseek-v4-pro) ──► 回答
                        ▲
                  对话记忆（滑动窗口）
```

## 目录结构

```
.
├── app_file_loader.py   # 命令行/测试：文档入库
├── app_chat.py          # Web 界面：对话问答
├── config_data.py       # 全部配置（模型名、路径、检索参数）
├── loader_factory.py    # 加载器工厂（按扩展名分发）
├── knowledge_base.py    # 向量库操作（入库/检索/查询）
├── qa_service.py        # 问答服务（检索+LLM+视觉）
├── memory.py            # 对话记忆（滑动窗口）
├── loaders/             # 各类文档解析器
│   ├── base_loader.py   #   抽象基类
│   ├── docx_loader.py
│   ├── pdf_loader.py
│   ├── txt_loader.py
│   └── excel_loader.py
└── runtime/             # 运行时数据（自动生成，不入库）
    ├── chroma_db/       #   向量库
    ├── output/          #   提取的图片
    └── file_md5.txt     #   去重记录
```

## 快速开始

### 1. 环境要求

- Python 3.9+
- 具备两个模型服务的 API Key（见下）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 API Key（必做）

本项目不内置任何 Key，模型服务通过**环境变量**鉴权：

```bash
# Windows PowerShell
$env:DASHSCOPE_API_KEY = "你的通义千问Key"
$env:DEEPSEEK_API_KEY  = "你的DeepSeek Key"

# Linux / macOS
export DASHSCOPE_API_KEY="你的通义千问Key"
export DEEPSEEK_API_KEY="你的DeepSeek Key"
```

| 环境变量 | 用途 | 获取地址 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 嵌入模型 + 视觉模型（阿里云百炼） | https://bailian.console.aliyun.com |
| `DEEPSEEK_API_KEY` | 对话模型（DeepSeek 开放平台） | https://platform.deepseek.com |

### 4. 修改模型（可选）

默认模型在 `config_data.py` 顶部，按需改成你自己开通的模型：

```python
EMBEDDING_MODEL = "text-embedding-v4"   # 嵌入模型（通义千问）
CHAT_MODEL      = "deepseek-v4-pro"     # 对话模型（DeepSeek）
VISION_MODEL    = "qwen-vl-max"         # 视觉模型（通义千问）
```

### 5. 使用

**① 文档入库**（把文档丢进知识库）：

```bash
python app_file_loader.py
# 按提示输入文档路径，支持 .docx / .pdf / .txt / .xlsx
```

**② 启动问答界面**（浏览器打开 Gradio）：

```bash
python app_chat.py
# 浏览器访问 http://127.0.0.1:7860
```

## 常见问题 FAQ

**Q1：入库报错提示缺少 API Key？**
检查是否已设置 `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY`，PowerShell 用 `$env:变量名 = "..."`，设置后需重新打开终端或在同一终端运行。

**Q2：想换其他模型？**
修改 `config_data.py` 顶部的 `EMBEDDING_MODEL` / `CHAT_MODEL` / `VISION_MODEL`，前提是你的 Key 有对应模型的调用权限。

**Q3：重复入库同一份文件？**
系统按文件 MD5 自动去重，同一文件重复提交会提示已存在。

**Q4：runtime/ 目录会自动生成？**
是的，向量库、提取图片、去重记录都在 `runtime/` 下自动生成，已被 `.gitignore` 忽略，删除该目录不影响代码。

## 项目演进

| 版本 | 内容 |
|---|---|
| v1.0 | 仅支持 docx 解析入库 + 基础问答 |
| v2.0 | 支持 pdf/txt/excel；表格感知解析；文件 MD5 去重；对话记忆；Gradio 界面 |

## License

[MIT](./LICENSE)
