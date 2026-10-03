# ========== 第 1 行：选地基（Python 3.11 精简版 Linux）==========
FROM python:3.11-slim

# ========== 第 2 行：工作目录（之后所有命令都在 /app 里执行）==========
WORKDIR /app

# ========== 第 3~4 行：先拷依赖清单，装依赖（清单没变则缓存命中，秒级重构建）==========
# -i 指定清华 pip 镜像源：容器内直连 PyPI 官方源在国内极慢/超时（实测 chromadb 下载 740s 超时）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# ========== 第 5 行：再拷整个项目代码 ==========
COPY . .

# ========== 第 6 行：声明容器开放 8000 端口（真正对外开放靠 compose 的 ports）==========
EXPOSE 8000

# ========== 第 7 行：容器启动后执行 = 你平时手动敲的 uvicorn 命令 ==========
CMD ["uvicorn", "app_api:app", "--host", "0.0.0.0", "--port", "8000"]
