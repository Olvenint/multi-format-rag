"""
docx 文档知识库 — AI 问答界面

运行：python app_chat.py

功能：
    - 用户输入问题 → DocxQAService.ask_stream() 流式检索 + 回答
    - 图片描述在入库时已预计算，查询时零视觉 API 调用
    - LLM 逐 token 流式输出，打字机效果
    - 对话历史持久化到 conversation_memory.jsonl
    - 支持多轮追问（滑动窗口记忆，最近 N 轮注入上下文）

调用链：
    用户输入
      → respond()                        前端回调
      → DocxQAService.ask_stream()
          → memory.get_history_text()     取最近 N 轮历史
          → DocxRetriever.retrieve()      向量检索 + 读取预计算图片描述
          → _format_context()             历史 + 检索结果拼成上下文
          → chain.stream()                LLM 逐 token 流式生成
          → memory.save()                持久化本轮问答
      → 前端逐 token 更新，打字机效果
"""
import gradio as gr
from qa_service import DocxQAService


# ============================================================
# CSS 样式（风格参考 app_file_loader.py）
# ============================================================
CUSTOM_CSS = """
/* 整体背景与字体 */
body, .gradio-container {
    background: #f5faf7 !important;
    font-family: "Segoe UI", "微软雅黑", sans-serif;
    max-width: 880px !important;
    margin: 0 auto !important;
}

/* 标题区 */
.header-box {
    text-align: center; padding: 24px 0 12px 0;
}
.header-box h1 {
    font-size: 2em; color: #2d6a4f; margin: 0;
    letter-spacing: 2px;
}
.header-box p {
    color: #52b788; margin: 6px 0 0 0; font-size: 0.95em;
}

/* 聊天容器 */
.chat-card {
    background: #ffffff;
    border-radius: 18px;
    padding: 16px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    border: 1px solid #e8f5e9;
}

/* 输入区 */
.input-card {
    background: #ffffff;
    border-radius: 14px;
    padding: 10px 14px;
    margin-top: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
    border: 1px solid #e8f5e9;
}

/* 主按钮 */
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

/* 次按钮 */
button.clear-btn {
    background: #fff !important;
    border: 1px solid #d0d0d0 !important;
    color: #888 !important;
    font-weight: 500 !important; border-radius: 12px !important;
    padding: 10px 24px !important; font-size: 0.95em !important;
    transition: all 0.2s !important;
}
button.clear-btn:hover {
    border-color: #52b788 !important;
    color: #52b788 !important;
}

/* 动画 */
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
    padding: 16px 0 4px 0;
}
footer { display: none !important; }
"""


# ============================================================
# 图标 SVG（同 app_file_loader.py）
# ============================================================
LEAF_SVG = """
<svg width="32" height="32" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M17 8C8 10 5.9 16.17 3.82 21.34L5.71 22L6.66 19.7C7.14 19.42 7.71 19.35 8.24 19.53C10.58 20.35 13.67 20.12 15.87 18.55C18.62 16.59 20.23 13.11 19.99 9.5C19.93 8.46 19.41 7.5 18.62 6.88C17.83 6.26 16.84 6.04 15.88 6.27C14.02 6.72 12.33 7.5 10.89 8.53" stroke="#52b788" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""


# ============================================================
# 全局 QA 服务（延迟初始化，避免启动时连接数据库）
# ============================================================
_qa_service = None

def get_qa_service():
    """单例模式：首次调用时初始化 DocxQAService"""
    global _qa_service
    if _qa_service is None:
        _qa_service = DocxQAService()
    return _qa_service


# ============================================================
# 聊天回调
# ============================================================
def respond(message, chat_history):
    """
    用户发送消息 → AI 检索知识库 + 记忆 + LLM 流式回答

    三阶段流式更新：
        1. 显示 "检索中" 占位 → 用户立刻看到反馈
        2. ask_stream() 逐 token yield → 打字机效果
        3. 完成后自动保存到记忆

    参数：
        message:      用户输入的文本
        chat_history: 当前对话历史（messages 格式，List[dict]）
    """
    if not message.strip():
        yield "", chat_history
        return

    # 阶段 1：显示用户消息 + "检索中" 占位
    chat_history.append({"role": "user", "content": message})
    chat_history.append({"role": "assistant", "content": "⏳ 正在检索知识库..."})
    yield "", chat_history

    # 阶段 2：流式生成回答（逐 token 更新，打字机效果）
    try:
        qa = get_qa_service()
        accumulated = ""
        for chunk in qa.ask_stream(message):
            accumulated += chunk
            chat_history[-1] = {"role": "assistant", "content": accumulated}
            yield "", chat_history
    except Exception as e:
        chat_history[-1] = {"role": "assistant", "content": f"❌ 回答失败：{str(e)[:100]}"}
        yield "", chat_history


def clear_chat():
    """清空对话显示和记忆文件，重新开始"""
    global _qa_service
    if _qa_service is not None:
        _qa_service.memory.clear()
    return "", []


# ============================================================
# 构建界面
# ============================================================
with gr.Blocks(title="智能问答") as app:
    # ---------- 顶部标题 ----------
    gr.HTML(f"""
    <div class="header-box animate-in">
        <div style="display:flex;align-items:center;justify-content:center;gap:8px;">
            {LEAF_SVG}
            <h1>智能问答</h1>
        </div>
        <p>基于知识库的 AI 文档问答 · 支持多轮对话记忆</p>
    </div>
    """)

    # ---------- 聊天区 ----------
    with gr.Column(elem_classes="chat-card animate-in"):
        chatbot = gr.Chatbot(
            value=[{"role": "assistant",
                    "content": "👋 你好！我是文档问答助手，可以基于知识库回答你的问题"}],
            height=480,
            show_label=False,
        )

    # ---------- 输入区 ----------
    with gr.Row(elem_classes="input-card animate-in"):
        msg = gr.Textbox(
            placeholder="请输入你的问题，按 Enter 发送...",
            show_label=False,
            scale=8,
            lines=1,
            container=False,
        )
        send_btn = gr.Button("发送", variant="primary", elem_classes="primary-btn", scale=1)
        clear_btn = gr.Button("清空", elem_classes="clear-btn", scale=1)

    # ---------- 事件绑定 ----------
    send_btn.click(fn=respond, inputs=[msg, chatbot], outputs=[msg, chatbot])
    msg.submit(fn=respond, inputs=[msg, chatbot], outputs=[msg, chatbot])
    clear_btn.click(fn=clear_chat, outputs=[msg, chatbot])

    # ---------- Footer ----------
    gr.HTML('<div class="footer">Powered by LangChain + Chroma + DeepSeek | RAG with Memory</div>')


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    app.queue().launch(
        server_name="127.0.0.1",
        server_port=7861,
        inbrowser=True,
        show_error=True,
        css=CUSTOM_CSS,
        theme=gr.themes.Soft(primary_hue="emerald"),
    )
