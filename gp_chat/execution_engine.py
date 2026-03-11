import pandas as pd
import numpy as np
import matplotlib
# GUIバックエンドを使わない設定（サーバーでのクラッシュ防止）
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import io
import contextlib
import traceback
import sys
import platform
import os
import ast
import tempfile
import multiprocessing
import queue  # Timeout時のEmpty例外をキャッチするために追加

# --- セキュリティ設定 (最低限の制限) ---
FORBIDDEN_IMPORTS = {"os", "subprocess", "sys", "shutil", "pty", "socket"}
FORBIDDEN_STRINGS = ["169.254.169.254", "metadata.google.internal"]

def _check_security(code: str):
    """実行前にコードの安全性を静的に検証する"""
    # 1. 文字列の直接マッチングでメタデータサーバーへのアクセス試行をブロック
    for f_str in FORBIDDEN_STRINGS:
        if f_str in code:
            raise ValueError(f"Security Error: Cloud metadata access is forbidden. ({f_str})")

    # 2. AST(抽象構文木)を使って、禁止されたモジュールのインポートをブロック
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] in FORBIDDEN_IMPORTS:
                        raise ValueError(f"Security Error: Import of '{alias.name}' is forbidden.")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] in FORBIDDEN_IMPORTS:
                    raise ValueError(f"Security Error: Import from '{node.module}' is forbidden.")
    except SyntaxError as e:
        raise ValueError(f"Syntax Error: {e}")


def setup_japanese_font():
    """OSに応じた日本語フォントを設定し、グラフの文字化けを防ぐ"""
    os_name = platform.system()
    font_name = "sans-serif" # デフォルト

    if os_name == "Windows":
        candidates = ["Meiryo", "Yu Gothic", "MS Gothic"]
        for f in candidates:
            try:
                matplotlib.font_manager.findfont(f, fallback_to_default=False)
                font_name = f
                break
            except:
                continue
    elif os_name == "Darwin": # macOS
        font_name = "Hiragino Sans"
    else: # Linux / Streamlit Cloud / Docker
        candidates = ["Noto Sans CJK JP", "IPAexGothic", "IPAGothic", "VL Gothic"]
        for f in candidates:
             try:
                matplotlib.font_manager.findfont(f, fallback_to_default=False)
                font_name = f
                break
             except:
                continue

    matplotlib.rcParams['font.family'] = font_name
    matplotlib.rcParams['axes.unicode_minus'] = False


def _worker_process(code: str, file_paths: dict, canvases: list, result_queue: multiprocessing.Queue):
    """
    別プロセスで実際にコードを実行するワーカー関数
    """
    setup_japanese_font()
    
    buffer = io.StringIO()
    figures_bytes = []
    
    # exec に渡す名前空間（関数内からの参照エラーを防ぐため統一する）
    local_scope = {
        "pd": pd,
        "plt": plt,
        "io": io,
        "np": np,
    }
    
    # ファイルパスとCanvasの注入
    local_scope['files'] = file_paths
    for i, content in enumerate(canvases):
        local_scope[f"canvas_{i+1}"] = content

    original_cwd = os.getcwd()

    try:
        # 3. 作業ディレクトリを一時フォルダに隔離
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            
            try:
                plt.close('all')
                
                # ユーザーが plt.show() を書いても安全にスキップ（無視）させる
                def _mock_show(*args, **kwargs):
                    pass
                plt.show = _mock_show
                
                # 標準出力と標準エラー出力をバッファにリダイレクト
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    # localsとglobalsに同じ辞書を渡して自然なスコープにする
                    exec(code, local_scope, local_scope)
                    
                    fig_nums = plt.get_fignums()
                    if fig_nums:
                        print(f"[System] {len(fig_nums)} charts generated.") 
                        for i in fig_nums:
                            fig = plt.figure(i)
                            img_buf = io.BytesIO()
                            fig.savefig(img_buf, format='png', bbox_inches='tight')
                            img_buf.seek(0)
                            figures_bytes.append(img_buf.getvalue())
                    else:
                        print("[System] No charts generated.")
            finally:
                os.chdir(original_cwd)

        # 実行成功時の結果をキューに格納
        result_queue.put({"stdout": buffer.getvalue(), "figures": figures_bytes})

    except Exception:
        # 実行時エラーの場合もトレースバックをバッファに書き込んで返す
        traceback.print_exc(file=buffer)
        result_queue.put({"stdout": buffer.getvalue(), "figures": figures_bytes})


def execute_user_code(code: str, file_paths: dict, canvases: list, timeout: int = 30):
    """
    AIが生成したPythonコードを安全に実行し、標準出力とグラフ画像(io.BytesIO)を返します。
    """
    # 事前に静的セキュリティチェック
    try:
        _check_security(code)
    except ValueError as e:
        return f"[Security Error]\n{e}", []

    result_queue = multiprocessing.Queue()
    
    p = multiprocessing.Process(target=_worker_process, args=(code, file_paths, canvases, result_queue))
    p.start()
    
    # デッドロック回避: p.join() の前にデータを取得(get)する
    try:
        # 子プロセスの処理完了(あるいはエラー)をタイムアウト付きで待機
        res = result_queue.get(timeout=timeout)
        p.join(1) # 残りの終了プロセスを少しだけ待つ
    except queue.Empty:
        # タイムアウト時間内にデータが来なかった場合はプロセスを強制終了
        if p.is_alive():
            p.terminate()
            p.join()
        return f"[Execution Timeout]\nCode execution exceeded the {timeout} seconds limit. Process was terminated.", []

    # 正常終了または例外発生時の結果取得
    if res:
        # バイト列として受け取った画像データを io.BytesIO に復元
        figures = [io.BytesIO(b) for b in res["figures"]]
        return res["stdout"], figures
    else:
        return "[System Error]\nWorker process terminated unexpectedly without returning data.", []