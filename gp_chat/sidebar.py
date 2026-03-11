import streamlit as st
import os
import json
import time
import io
import datetime
import hashlib
import copy  # ★追加: ディープコピー用
from streamlit_ace import st_ace
from streamlit_paste_button import paste_image_button
from . import config
from . import firestore_utils

# ★修正: handle_review, handle_validation の引数を削除
def render_sidebar(supported_types, env_files, load_history, handle_clear, handle_file_upload, user_uid="unknown"):
    with st.sidebar:
        st.header("AIモデル選択")

        st.selectbox(
            label="Environment (.env)",
            options=env_files,
            format_func=lambda x: os.path.basename(x),
            key='selected_env_file',
            disabled=st.session_state.get('is_generating', False)
        )

        st.selectbox("Target Model", config.AVAILABLE_MODELS, key='current_model_id')
        st.selectbox("Thinking level", ['high', 'low'], key='reasoning_effort')
        st.checkbox(config.UITexts.WEB_SEARCH_LABEL, key='enable_google_search')
        st.divider()

        # --- コールバック関数を使用してリセット処理を行う ---
        def reset_conversation():
            # 1. パスワードと認証状態を一時退避
            saved_pass = st.session_state.get('encryption_password', "")
            saved_valid = st.session_state.get('is_password_valid', False)
            saved_input = st.session_state.get('enc_pass_input', "")

            # 2. 会話の初期化 (デフォルト設定のロード)
            # ★修正: 参照渡しによる汚染を防ぐため deepcopy を使用
            for k, v in config.SESSION_STATE_DEFAULTS.items(): 
                st.session_state[k] = copy.deepcopy(v)

            # 3. 退避したパスワード状態を復元
            st.session_state['encryption_password'] = saved_pass
            st.session_state['is_password_valid'] = saved_valid
            st.session_state['enc_pass_input'] = saved_input
            
            # アップローダーのリセットトリガー
            st.session_state['clear_uploader'] = True

        st.button("会話をリセット", on_click=reset_conversation)

        # --- セキュリティ設定 (1ユーザー1パスワード固定) ---
        st.header("セキュリティ設定 (Firestore)")
        
        if 'encryption_password' not in st.session_state:
            st.session_state['encryption_password'] = ""
        if 'is_password_valid' not in st.session_state:
            st.session_state['is_password_valid'] = False

        st.caption("1ユーザーにつき1つの暗号化キーを固定して使用します。")
        
        # 入力されたパスワードを即時検証する関数
        def on_password_change():
            enc_pass = st.session_state['enc_pass_input']
            if enc_pass:
                is_valid = firestore_utils.verify_crypto_password(user_uid, enc_pass)
                st.session_state['is_password_valid'] = is_valid
                st.session_state['encryption_password'] = enc_pass
            else:
                st.session_state['is_password_valid'] = False
                st.session_state['encryption_password'] = ""

        st.text_input(
            "暗号化キー (初回入力で固定されます)", 
            type="password", 
            key="enc_pass_input",
            on_change=on_password_change
        )
        
        if st.session_state['encryption_password']:
            if st.session_state['is_password_valid']:
                st.success("✅ 認証成功: 暗号化保存・復号が有効です")
            else:
                st.error("❌ パスワードが間違っています。既存履歴は文字化けします。")
        else:
            st.warning("未入力のため、新規チャットは平文で保存されます。")

        # 認証成功時のみ暗号化ONとする
        st.session_state['use_encryption'] = st.session_state['is_password_valid']

        # --- 過去のチャット履歴 (Cloud Storage連動版) ---
        st.subheader("過去のチャット履歴")
        if user_uid != "unknown":
            # 正しいパスワードが入力されている時のみ復号用キーを渡す
            decryption_pass = st.session_state['encryption_password'] if st.session_state['is_password_valid'] else ""
            histories = firestore_utils.get_history_list(user_uid, decryption_pass)
            
            if histories:
                hist_titles = {h["title"]: h for h in histories}
                selected_title = st.selectbox("履歴を選択", ["--- 選択してください ---"] + list(hist_titles.keys()))
                
                if selected_title != "--- 選択してください ---" and st.button("クラウドから履歴を復元"):
                    raw_data = hist_titles[selected_title]["raw_data"]
                    storage_path = raw_data.get("storage_path")
                    is_encrypted = raw_data.get("is_encrypted", False)
                    
                    if not storage_path:
                        st.error("旧フォーマットの履歴です。Storageパスが見つかりません。")
                        st.stop()
                    
                    if is_encrypted and not st.session_state['is_password_valid']:
                        st.error("暗号化されています。正しい暗号化キーを入力してください。")
                        st.stop()
                        
                    try:
                        with st.spinner("クラウドからデータをダウンロード中..."):
                            loaded_data = firestore_utils.load_chat_from_cloud(
                                storage_path=storage_path, 
                                is_encrypted=is_encrypted, 
                                password=st.session_state['encryption_password']
                            )
                            
                        # JSONからのセッションリフレッシュ設計
                        if isinstance(loaded_data, dict) and "messages" in loaded_data:
                            # 1. 【上書き】SNAPSHOT_KEYS に基づき、保存された全設定を復元
                            for key in config.SNAPSHOT_KEYS:
                                if key in loaded_data:
                                    st.session_state[key] = copy.deepcopy(loaded_data[key])
                                    
                            st.session_state['chat_title'] = raw_data.get("display_title", selected_title)
                            
                            # 2. 【破棄】
                            st.session_state['uploaded_file_queue'] = []
                            st.session_state['clipboard_queue'] = []
                            st.session_state['debug_logs'] = []
                            st.session_state['is_generating'] = False
                            st.session_state['clear_uploader'] = True
                            
                            st.session_state['system_role_defined'] = True
                            st.success("履歴を復元しました！")
                            st.rerun()
                    except Exception as e:
                        st.error(f"復元に失敗しました: {e}")
            else:
                st.caption("保存された履歴はありません。")
        else:
            st.caption("※ユーザー認証情報の取得に失敗しました。")

        st.divider()

        # --- History (ローカル用ダウンロード/アップロード) ---
        st.subheader("ローカル保存・復元")
        
        # ★修正: SNAPSHOT_KEYS に基づき、全設定情報を含めた完全なダウンロードデータを構築
        download_dict = {
            key: copy.deepcopy(st.session_state[key])
            for key in config.SNAPSHOT_KEYS
            if key in st.session_state
        }
        download_data = json.dumps(download_dict, ensure_ascii=False, indent=2)
        
        dl_filename = st.session_state.get('chat_title') or f"chat_history_{time.strftime('%y%m%d_%H%M%S')}.json"
        if not dl_filename.endswith('.json'):
            dl_filename += '.json'
            
        st.download_button(
            label="現在の会話をJSONでダウンロード",
            data=download_data,
            file_name=dl_filename,
            mime="application/json",
            use_container_width=True
        )

        st.file_uploader("JSONから会話を再開", type="json", key="hist_up", on_change=load_history, args=("hist_up",))
        st.divider()
        
        st.header("ファイルを添付")
        
        # キューとアップローダーリセット用の初期化
        if "uploaded_file_queue" not in st.session_state: st.session_state["uploaded_file_queue"] = []
        if "clipboard_queue" not in st.session_state: st.session_state["clipboard_queue"] = []
        if "uploader_key_counter" not in st.session_state: st.session_state["uploader_key_counter"] = 0
        
        # main.py等からフラグが渡されたらカウンターを更新してキーを変更(リセット)する
        if st.session_state.get('clear_uploader', False):
            st.session_state["uploader_key_counter"] += 1
            st.session_state['clear_uploader'] = False

        # 動的なキーを持たせることで強制的に空状態のコンポーネントを再描画する
        up_key = f"main_up_{st.session_state['uploader_key_counter']}"
        uf = st.file_uploader("アップロード", type=["png","pdf","docx","pptx","py","txt","bat"], accept_multiple_files=True, key=up_key)
        
        if uf: 
            st.session_state["uploaded_file_queue"] = uf
        else: 
            st.session_state["uploaded_file_queue"] = []

        # --- クリップボードからの画像ペースト機能 ---
        class VirtualUploadedFile:
            def __init__(self, data_bytes, name, mime_type):
                self._data = data_bytes
                self.name = name
                self.type = mime_type
            
            def getvalue(self):
                return self._data

        paste_result = paste_image_button(
            label="📋 クリップボード画像を追加",
            text_color="#000000",
            background_color="#f0f2f6",
            hover_background_color="#e0e2e6",
            errors="ignore"
        )

        if paste_result.image_data is not None:
            buf = io.BytesIO()
            paste_result.image_data.save(buf, format='PNG')
            byte_data = buf.getvalue()
            
            img_hash = hashlib.md5(byte_data).hexdigest()
            if st.session_state.get('last_pasted_hash') != img_hash:
                st.session_state['last_pasted_hash'] = img_hash
                timestamp = datetime.datetime.now().strftime("%H%M%S")
                filename = f"clipboard_{timestamp}.png"
                
                virtual_file = VirtualUploadedFile(byte_data, filename, "image/png")
                st.session_state['clipboard_queue'].append(virtual_file)
                st.toast(f"画像を追加しました: {filename}", icon="✅")

        # --- 送信待ちファイルの表示部分 ---
        total_files = len(st.session_state['uploaded_file_queue']) + len(st.session_state['clipboard_queue'])
        
        if total_files > 0:
            st.markdown(f"**送信待ち: {total_files} 件**")
            
            if st.session_state['clipboard_queue']:
                st.caption("クリップボード取得分:")
                for i, vfile in enumerate(st.session_state['clipboard_queue']):
                    col_del, col_name = st.columns([1, 5])
                    with col_del:
                        if st.button("❌", key=f"del_clip_{i}_{st.session_state['uploader_key_counter']}"):
                            st.session_state['clipboard_queue'].pop(i)
                            st.session_state['last_pasted_hash'] = None
                            st.rerun()
                    with col_name:
                        st.text(vfile.name)
        else:
            st.caption("ファイルは選択されていません")

        st.divider()
        
        # --- 追加機能: グラフ描画・データ分析モード ---
        st.subheader("分析・実行オプション")
        
        # Widget競合バグの解消: value+rerunを廃止し、keyバインディングのみで状態を直接管理する
        st.checkbox(
            label="📈 グラフ描画・データ分析 (Python自動実行)", 
            help="ONにすると、AIが生成したPythonコードをサーバー上で実行し、結果をチャットに返します。\nアップロードファイルは `files['ファイル名']` でアクセス可能です。",
            key="auto_plot_enabled" 
        )

        st.divider()

        st.subheader(config.UITexts.EDITOR_SUBHEADER)
        
        # こちらも競合バグの解消: keyバインディングのみとする
        st.checkbox(config.UITexts.MULTI_CODE_CHECKBOX, key="multi_code_enabled")

        canvases = st.session_state['python_canvases']
        if st.session_state.get('multi_code_enabled', False):
            if len(canvases) < config.MAX_CANVASES and st.button(config.UITexts.ADD_CANVAS_BUTTON, use_container_width=True):
                canvases.append(config.ACE_EDITOR_DEFAULT_CODE)
                st.rerun()
            
            for i, content in enumerate(canvases):
                st.write(f"**Canvas-{i + 1}**")
                ace_key = f"ace_{i}_{st.session_state['canvas_key_counter']}"
                updated = st_ace(value=content, key=ace_key, **config.ACE_EDITOR_SETTINGS, auto_update=True)
                if updated != content:
                    canvases[i] = updated
                
                # ★修正: カラム分割とレビュー・検証ボタンを削除し、クリアボタンのみにする
                st.button(config.UITexts.CLEAR_BUTTON, key=f"clr_{i}", on_click=handle_clear, args=(i,), use_container_width=True)

                c_up_key = f"up_{i}_{st.session_state['canvas_key_counter']}"
                st.file_uploader(f"Load into Canvas-{i+1}", type=supported_types, key=c_up_key, on_change=handle_file_upload, args=(i, c_up_key))
                st.divider()
        else:
            if len(canvases) > 1:
                st.session_state['python_canvases'] = [canvases[0]]
                st.rerun()
            
            ace_key = f"ace_single_{st.session_state['canvas_key_counter']}"
            updated = st_ace(value=canvases[0], key=ace_key, **config.ACE_EDITOR_SETTINGS, auto_update=True)
            if updated != canvases[0]:
                canvases[0] = updated

            # ★修正: カラム分割とレビュー・検証ボタンを削除し、クリアボタンのみにする
            st.button(config.UITexts.CLEAR_BUTTON, key="clr_s", on_click=handle_clear, args=(0,), use_container_width=True)
            
            c_up_key = f"up_s_{st.session_state['canvas_key_counter']}"
            st.file_uploader("Load into Canvas", type=supported_types, key=c_up_key, on_change=handle_file_upload, args=(0, c_up_key))
            
        st.markdown("---")
        st.markdown(
            """
            <div style="text-align: center; font-size: 12px; color: #666;">
                Powered by <a href="https://github.com/yoichi-1984/GP-chat_on_GCP" target="_blank" style="color: #666;">GP-Chat_on_GCP</a><br>
                © yoichi-1984<br>
                Licensed under <a href="https://www.apache.org/licenses/LICENSE-2.0" target="_blank" style="color: #666;">Apache 2.0</a>
            </div>
            """,
            unsafe_allow_html=True
        )