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

def execute_user_code(code: str, file_paths: dict, canvases: list):
    """
    AIが生成したPythonコードを実行し、標準出力とグラフ画像(io.BytesIO)を返します。
    """
    setup_japanese_font()
    
    buffer = io.StringIO()
    figures = []
    
    # 実行スコープ（Local Scope）の準備
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

    try:
        plt.close('all')
        
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            exec(code, {}, local_scope)
            
            fig_nums = plt.get_fignums()
            if fig_nums:
                print(f"[System] {len(fig_nums)} charts generated.") 
                for i in fig_nums:
                    fig = plt.figure(i)
                    img_buf = io.BytesIO()
                    fig.savefig(img_buf, format='png', bbox_inches='tight')
                    img_buf.seek(0)
                    figures.append(img_buf)
            else:
                print("[System] No charts generated.")

    except Exception:
        traceback.print_exc(file=buffer)
    
    return buffer.getvalue(), figures