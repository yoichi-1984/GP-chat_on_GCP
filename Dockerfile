FROM python:3.11-slim

WORKDIR /app

# システムパッケージ
RUN apt-get update && apt-get install -y curl fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

# 設定ファイルコピー
COPY pyproject.toml .
COPY . .

# 依存関係インストール (pyproject.tomlの内容が入ります)
RUN pip install --no-cache-dir .

# Cloud Run設定
RUN mkdir -p env && touch env/cloud_run.env
ENV PORT=8080
ENV STREAMLIT_PORT=8501

# ★変更点: Streamlitではなく、FastAPIサーバー(server.py)を起動
CMD exec uvicorn server:app --host 0.0.0.0 --port $PORT --workers 1