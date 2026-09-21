"""
文档知识库 — 上传界面（支持 docx / pdf / txt / excel）

运行：python app_file_loader.py
"""
import os
import time
import gradio as gr 
from loader_factory import LoaderFactory
from knowledge_base import KnowledgeBase

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- CSS 样式 ----------
CUSTOM_CSS = """
/* 整体背景与字体 */
body, .gradio-container {
    background: #f5faf7 !important;
    font-family: "Segoe UI", "微软雅黑", sans-serif;
}

/* 标题区 */
.header-box {
    text-align: center; padding: 20px 0 8px 0;
}
.header-box h1 {
    font-size: 2em; color: #2d6a4f; margin: 0;
    letter-spacing: 2px;
}
.header-box p {
    color: #52b788; margin: 4px 0 0 0; font-size: 0.95em;
}

/* 上传区 */
.upload-card {
    background: #ffffff;
    border: 2px dashed #95d5b2;
    border-radius: 18px;
    padding: 30px 20px;
    text-align: center;
    transition: border-color 0.3s, box-shadow 0.3s;
}
.upload-card:hover {
    border-color: #52b788;
    box-shadow: 0 0 0 4px rgba(82,183,136,0.12);
}

/* 按钮 */
button.primary-btn {
    background: linear-gradient(135deg, #52b788, #40916c) !important;
    border: none !important; color: #fff !important;
    font-weight: 600 !important; border-radius: 12px !important;
    padding: 10px 28px !important; font-size: 1em !important;
    transition: transform 0.2s, box-shadow 0.2s !important;
}
button.primary-btn:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(45,106,79,0.3) !important;
}

/* 结果卡片 */
.result-card {
    background: #ffffff; border-radius: 14px;
    padding: 18px 22px; margin-top: 12px;
    border-left: 5px solid #52b788;
    box-shadow: 0 2px 10px rgba(0,0,0,0.04);
}

/* 统计数字 */
.stat-badge {
    display: inline-block; background: #e8f5e9;
    color: #2d6a4f; border-radius: 20px;
    padding: 4px 14px; margin: 3px 6px;
    font-weight: 600; font-size: 0.9em;
}

/* 动画 — 淡入 */
@keyframes fadeSlide {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
}
.animate-in {
    animation: fadeSlide 0.45s ease-out;
}

/* Footer */
.footer {
    text-align: center; color: #aaa; font-size: 0.8em;
    padding: 20px 0 4px 0;
}

/* 隐藏 Gradio 默认 footer */
footer { display: none !important; }
"""

# ---------- 图标 SVG ----------
LEAF_SVG = """
<svg width="32" height="32" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M17 8C8 10 5.9 16.17 3.82 21.34L5.71 22L6.66 19.7C7.14 19.42 7.71 19.35 8.24 19.53C10.58 20.35 13.67 20.12 15.87 18.55C18.62 16.59 20.23 13.11 19.99 9.5C19.93 8.46 19.41 7.5 18.62 6.88C17.83 6.26 16.84 6.04 15.88 6.27C14.02 6.72 12.33 7.5 10.89 8.53" stroke="#52b788" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""

CHECK_SVG = """
<svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <circle cx="12" cy="12" r="10" fill="#52b788"/>
  <path d="M7 12.5L10.5 16L17 9" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


# ---------- 核心处理函数 ----------
def process_and_store(file_obj):
    """上传回调：解析 + 入库，返回 HTML 结果（支持多格式）"""
    if file_obj is None:
        return "<div style='color:#aaa;text-align:center;padding:30px;'>等待上传文件...</div>"

    # Gradio 6.0 的 File 组件返回文件路径字符串
    tmp_path = str(file_obj)
    filename = os.path.basename(tmp_path)

    # 检查格式是否支持
    if not LoaderFactory.is_supported(tmp_path):
        supported = ", ".join(LoaderFactory.supported_extensions())
        return f"<div style='color:#e74c3c;text-align:center;padding:30px;'>不支持的文件格式<br><span style='color:#888;font-size:0.9em;'>支持格式：{supported}</span></div>"

    # ---- Step 1: 解析（用工厂按扩展名分发到对应 Loader）----
    t0 = time.time()
    try:
        loader = LoaderFactory.create(tmp_path)
        documents = loader.load()
    except ImportError as e:
        return f"<div style='color:#e74c3c;padding:20px;'>解析失败：缺少依赖<br><span style='font-size:0.9em;'>{e}</span></div>"
    except Exception as e:
        return f"<div style='color:#e74c3c;padding:20px;'>解析失败：{str(e)[:200]}</div>"
    parse_time = round(time.time() - t0, 2)

    # ---- Step 2: 入库（通用 KnowledgeBase，支持增量更新）----
    t1 = time.time()
    kb = KnowledgeBase()
    result = kb.upload_documents(tmp_path, documents)
    store_time = round(time.time() - t1, 2)

    # ---- 构建 HTML 结果 ----
    inserted = result["inserted"]
    total = result["total"]
    deleted = result.get("deleted", 0)
    image_count = len(loader.extracted_images)
    msg = result.get("msg", "入库成功")

    # 统计各 doc_type 数量
    type_counts = {}
    for doc in documents:
        dt = doc.metadata.get("doc_type", "paragraph")
        type_counts[dt] = type_counts.get(dt, 0) + 1

    # 统计标签
    stats_parts = [
        f'<span class="stat-badge">📄 {total} 个语义块</span>',
    ]
    if image_count > 0:
        stats_parts.append(f'<span class="stat-badge">🖼 {image_count} 张图片</span>')
    for dt, cnt in type_counts.items():
        label = {"paragraph": "📝 文本", "table": "📊 表格", "sheet": "📋 Sheet"}.get(dt, dt)
        stats_parts.append(f'<span class="stat-badge">{label} {cnt}</span>')
    stats_parts.append(f'<span class="stat-badge">⏱ 解析 {parse_time}s</span>')
    stats_parts.append(f'<span class="stat-badge">💾 入库 {store_time}s</span>')
    if deleted > 0:
        stats_parts.append(f'<span class="stat-badge" style="background:#fff3cd;color:#856404;">🔄 更新 {deleted} 旧块</span>')
    stats_html = "".join(stats_parts)

    # 每个 chunk 的卡片
    chunks_html = ""
    for i, doc in enumerate(documents):
        heading = doc.metadata.get("heading", "无标题")
        chapter = doc.metadata.get("chapter", "")
        img_cnt = doc.metadata.get("image_count", 0)
        doc_type = doc.metadata.get("doc_type", "paragraph")
        preview = doc.page_content[:120].replace("\n", " ")
        if len(doc.page_content) > 120:
            preview += "..."

        # 类型标签
        type_badge = ""
        if doc_type == "table":
            rows = doc.metadata.get("table_rows", 0)
            cols = doc.metadata.get("table_cols", 0)
            type_badge = f'<span style="background:#dbeafe;color:#1e40af;border-radius:10px;padding:1px 8px;font-size:0.75em;">📊 表格 {rows}x{cols}</span>'
        elif doc_type == "sheet":
            type_badge = f'<span style="background:#fce7f3;color:#9d174d;border-radius:10px;padding:1px 8px;font-size:0.75em;">📋 Sheet</span>'
        if img_cnt > 0:
            type_badge += f' <span style="background:#ffd;color:#b45309;border-radius:10px;padding:1px 8px;font-size:0.75em;">📷 {img_cnt} 图</span>'

        chunks_html += f"""
        <div class="result-card animate-in" style="animation-delay:{i * 0.06}s">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
                <b style="color:#2d6a4f;">{heading}</b>
                {type_badge}
            </div>
            <div style="color:#888;font-size:0.8em;margin-bottom:6px;">
                章节: {chapter or '—'} &nbsp;|&nbsp; 长度: {len(doc.page_content)} 字符
            </div>
            <div style="color:#555;font-size:0.88em;line-height:1.6;">
                {preview}
            </div>
        </div>
        """

    return f"""
    <div class="animate-in">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
            {CHECK_SVG}
            <span style="font-size:1.2em;color:#2d6a4f;font-weight:600;">{msg}</span>
        </div>
        <div style="color:#666;margin-bottom:14px;">
            文件: <b>{filename}</b> &nbsp;|&nbsp; 已入库 {inserted} 块
        </div>
        <div style="margin-bottom:16px;">{stats_html}</div>
        <div style="max-height:420px;overflow-y:auto;padding-right:6px;">
            {chunks_html}
        </div>
    </div>
    """


# ---------- 构建界面 ----------
with gr.Blocks(title="文档知识库") as app:
    # 顶部
    gr.HTML(f"""
    <div class="header-box">
        <div style="display:flex;align-items:center;justify-content:center;gap:8px;">
            {LEAF_SVG}
            <h1>文档知识库</h1>
        </div>
        <p>上传 Word / PDF / TXT / Excel，自动解析语义结构并存入向量数据库</p>
    </div>
    """)

    with gr.Column(elem_classes="animate-in"):
        # 上传组件（支持多格式）
        upload = gr.File(
            label="上传文档（支持 .docx / .pdf / .txt / .md / .xlsx / .xls）",
            file_types=[".docx", ".pdf", ".txt", ".md", ".xlsx", ".xls"],
            file_count="single",
            elem_classes="upload-card",
        )

        # 提交按钮
        submit_btn = gr.Button("解析并入库", variant="primary", elem_classes="primary-btn")

    # 结果展示区
    result_html = gr.HTML(
        value="<div style='color:#aaa;text-align:center;padding:50px;font-size:0.95em;'>"
              "👆 选择或拖拽一个文档文件，然后点击「解析并入库」</div>"
    )

    # 绑定
    submit_btn.click(fn=process_and_store, inputs=upload, outputs=result_html)

    # Footer
    gr.HTML('<div class="footer">Powered by LangChain + Chroma | Multi-Format Document Loader (docx/pdf/txt/excel)</div>')


# ---------- 启动 ----------
if __name__ == "__main__":
    app.launch(
        server_name="127.0.0.1",
        server_port=7860,
        inbrowser=True,
        show_error=True,
        css=CUSTOM_CSS,
        theme=gr.themes.Soft(primary_hue="emerald"),
    )

    print(r'$p = (netstat -ano | Select-String ":7860" | Select-String "LISTENING"); if ($p) { $id = ($p -split "\s+")[-1]; Stop-Process -Id $id -Force }')
