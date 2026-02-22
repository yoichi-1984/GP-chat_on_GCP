# server.py:
import os
import subprocess
import asyncio
import sys
import json
import ipaddress

import httpx
import websockets
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect
import firebase_admin
from firebase_admin import auth

# --- ログ設定 ---
def log(msg):
    print(f"[Proxy] {msg}", flush=True)

# --- 設定読み込み ---

# 1. 環境変数からポートとURL設定を取得
STREAMLIT_PORT = int(os.getenv("STREAMLIT_PORT", 8501))
TARGET_URL = f"http://127.0.0.1:{STREAMLIT_PORT}"
# Cloud Run等の環境変数 HOSTING_URL がない場合はローカル開発用URLをデフォルトに
HOSTING_URL = os.getenv("HOSTING_URL", "http://localhost:8080")

# 2. JSONファイルから許可IPリストをロード
ALLOWED_NETWORKS = []
try:
    # Dockerコンテナ内では /app/ip_config.json に配置されます
    config_path = "ip_config.json"
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            for ip_str in config_data.get("allowed_networks", []):
                try:
                    ALLOWED_NETWORKS.append(ipaddress.ip_network(ip_str.strip()))
                except ValueError:
                    log(f"⚠️ Config Error: Invalid IP format '{ip_str}'")
        log(f"✅ Loaded {len(ALLOWED_NETWORKS)} allowed networks from {config_path}.")
    else:
        raise FileNotFoundError
except (FileNotFoundError, json.JSONDecodeError):
    log("⚠️ ip_config.json not found or invalid. Defaulting to localhost only for safety.")
    ALLOWED_NETWORKS.append(ipaddress.ip_network("127.0.0.1/32"))


# --- Firebase初期化 ---
if not firebase_admin._apps:
    try:
        firebase_admin.initialize_app()
        log("🔥 Firebase Admin Initialized")
    except Exception as e:
        log(f"⚠️ Firebase Init Error: {e}")

# --- ライフサイクル管理 (Streamlitの起動/停止) ---
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時処理
    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()

    # Streamlitをバックグラウンドで起動
    cmd = [
        "streamlit", "run", "main.py",
        "--server.port", str(STREAMLIT_PORT),
        "--server.headless", "true",
        "--server.address", "0.0.0.0",
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false",
        "--server.enableWebsocketCompression", "false",
        "--server.baseUrlPath", "", 
    ]
    log(f"🚀 Starting Streamlit: {' '.join(cmd)}")
    process = subprocess.Popen(cmd, env=env)

    # ヘルスチェック (Streamlitが立ち上がるまで待機)
    log("⏳ Waiting for Streamlit to be ready...")
    async with httpx.AsyncClient() as client:
        for i in range(30): # 最大30秒待機
            try:
                resp = await client.get(f"{TARGET_URL}/_stcore/health")
                if resp.status_code == 200:
                    log("✅ Streamlit is UP and Running!")
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        else:
            log("❌ Streamlit failed to start in time.")

    yield

    # 終了時処理
    log("🛑 Terminating Streamlit process...")
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

# FastAPIアプリ定義
app = FastAPI(lifespan=lifespan)
client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        HOSTING_URL,
        "http://localhost:8501",
        "http://127.0.0.1:8501",
        "http://localhost:8080"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ★IP制限ミドルウェア (CIDR対応版) ---
@app.middleware("http")
async def ip_restriction_middleware(request: Request, call_next):
    # Cloud Runでは 'X-Forwarded-For' の先頭がクライアントの真のIP
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip_str = forwarded.split(",")[0].strip()
    else:
        client_ip_str = request.client.host

    # ヘルスチェック(Cloud Run内部からのアクセス)は常に許可
    # User-AgentがGoogleHC (Google Health Check) の場合も考慮
    if request.url.path == "/_stcore/health" and (client_ip_str == "127.0.0.1" or "GoogleHC" in request.headers.get("User-Agent", "")):
        return await call_next(request)

    # IPアドレス判定
    try:
        client_ip = ipaddress.ip_address(client_ip_str)
        is_allowed = False

        for network in ALLOWED_NETWORKS:
            if client_ip in network:
                is_allowed = True
                break

        if not is_allowed:
            log(f"⛔ Access Denied: IP {client_ip_str} is NOT in allowed networks.")
            return JSONResponse(
                status_code=403, 
                content={"error": "Access Denied: Restricted Network (Corporate Proxy Only)."}
            )

    except ValueError:
        log(f"⚠️ Invalid IP Format received: {client_ip_str}")
        return JSONResponse(status_code=403, content={"error": "Invalid IP Address"})

    response = await call_next(request)
    return response


# --- 1. ログインAPI (Firebase Auth) ---
@app.post("/sessionLogin")
async def session_login(request: Request):
    try:
        body = await request.json()
        id_token = body.get("idToken")
        if not id_token:
            raise HTTPException(status_code=400, detail="ID token required")

        # セッションCookieの有効期限 (5日)
        expires_in = 60 * 60 * 24 * 5
        cookie = auth.create_session_cookie(id_token, expires_in=expires_in)

        resp = JSONResponse(content={"status": "success"})
        resp.set_cookie(
            key="session", 
            value=cookie, 
            httponly=True, 
            secure=True, # HTTPS必須
            max_age=expires_in, 
            samesite="none" # リダイレクトフローを考慮してLax推奨 (Noneの場合はSecure必須)
        )
        return resp
    except Exception as e:
        log(f"❌ Login Error: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed")

# --- 2. WebSocket プロキシ ---
@app.websocket("/{path:path}")
async def websocket_proxy(ws: WebSocket, path: str):
    # WebSocket接続前のHTTPハンドシェイクでIP制限は通過済み

    protocols = ws.headers.get("sec-websocket-protocol")
    subprotocol = protocols.split(",")[0].strip() if protocols else None
    await ws.accept(subprotocol=subprotocol)

    target_path = path if path else ""
    ws_url = f"ws://127.0.0.1:{STREAMLIT_PORT}/{target_path}"
    if ws.query_params:
        ws_url += f"?{ws.query_params}"

    try:
        async with websockets.connect(ws_url) as ws_server:
            # Client -> Server
            async def client_to_server():
                try:
                    while True:
                        data = await ws.receive()
                        if "text" in data:
                            await ws_server.send(data["text"])
                        elif "bytes" in data:
                            await ws_server.send(data["bytes"])
                except Exception:
                    pass

            # Server -> Client
            async def server_to_client():
                try:
                    while True:
                        data = await ws_server.recv()
                        if isinstance(data, str):
                            await ws.send_text(data)
                        else:
                            await ws.send_bytes(data)
                except Exception:
                    pass

            await asyncio.gather(client_to_server(), server_to_client())
    except Exception as e:
        log(f"WS Error: {e}")
    finally:
        try:
            await ws.close() 
        except:
            pass

# --- 3. HTTP プロキシ (メイン) ---
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy_handler(request: Request, path: str):
    # ログインAPIへのリクエストはプロキシしない
    if path == "sessionLogin":
        return await request.body() 

    # 静的ファイル判定 (Streamlitの仕様に基づく)
    is_static = (
        path.startswith("static/") or 
        path.startswith("_stcore/") or 
        path.startswith("vendor/") or
        path.endswith(".js") or 
        path.endswith(".css") or 
        path.endswith(".png") or
        path.endswith(".ico") or
        path.endswith(".svg") or
        path.endswith(".woff2")
    )

    # 認証チェック (静的ファイル以外)
    if not is_static:
        cookie = request.cookies.get("session")
        if not cookie:
            # ブラウザからのアクセスならログインページへ、APIなら401
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"{HOSTING_URL}/login/")
            return Response(status_code=401)
        try:
            auth.verify_session_cookie(cookie, check_revoked=True)
        except:
            return RedirectResponse(f"{HOSTING_URL}/login/")

    # リクエストの転送
    target_path = f"/{path}" if path else "/"
    url = f"{TARGET_URL}{target_path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # ヘッダーの整理 (ホスト等は書き換える)
    req_headers = dict(request.headers)
    req_headers.pop("host", None)
    req_headers.pop("content-length", None)

    try:
        body = await request.body()
        rp_req = client.build_request(request.method, url, headers=req_headers, content=body)
        r = await client.send(rp_req, stream=True)

        # 不要なヘッダーを除外してレスポンス
        excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded_headers}

        return StreamingResponse(
            r.aiter_bytes(), 
            status_code=r.status_code, 
            headers=headers, 
            background=r.aclose
        )
    except Exception as e:
        log(f"❌ HTTP Proxy Error: {url} -> {e}")
        return Response("Internal Proxy Error", status_code=500)