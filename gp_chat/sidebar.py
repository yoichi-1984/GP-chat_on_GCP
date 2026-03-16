# gp_chat/sidebar.py:
import streamlit as st
import os
import json
import time
import io
import datetime
import hashlib
import copy
from streamlit_ace import st_ace
from streamlit_paste_button import paste_image_button
from . import config
from . import firestore_utils
from . import session_state_manager

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

        def reset_conversation():
            session_state_manager.reset_conversation_state()

        st.button("会話をリセット", on_click=reset_conversation)

        st.header("セキュリティ設定 (Firestore)")

        if 'encryption_password' not in st.session_state:
            st.session_state['encryption_password'] = ""
        if 'is_password_valid' not in st.session_state:
            st.session_state['is_password_valid'] = False

        st.caption("1ユーザーにつき1つの暗号化キーを固定して使用します。")

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

        st.session_state['use_encryption'] = st.session_state['is_password_valid']

        # --- 過去のチャット履歴 (Cloud Storage連動版) ---
        st.subheader("過去のチャット履歴")

        # 復元処理を行うコールバック関数
        def restore_from_cloud_callback(raw_data, selected_title):
            storage_path = raw_data.get("storage_path")
            is_encrypted = raw_data.get("is_encrypted", False)

            if not storage_path:
                st.session_state['sidebar_msg'] = ("error", "旧フォーマットの履歴です。")
                return
            if is_encrypted and not st.session_state['is_password_valid']:
                st.session_state['sidebar_msg'] = ("error", "暗号化されています。正しい暗号化キーを入力してください。")
                return

            try:
                loaded_data = firestore_utils.load_chat_from_cloud(
                    storage_path=storage_path, 
                    is_encrypted=is_encrypted, 
                    password=st.session_state['encryption_password']
                )

                # 新モジュールで復元適用 (※Phase3まで chat_title 上書き処理はモジュール側で吸収されるためここでは行わない)
                session_state_manager.restore_snapshot(loaded_data)

                st.session_state['pending_restore_notice'] = "クラウドから履歴を復元しました！"
            except Exception as e:
                st.session_state['sidebar_msg'] = ("error", f"復元に失敗しました: {e}")

        # --- UI動的キーの適用 (Cloud selectbox) ---
        if user_uid != "unknown":
            decryption_pass = st.session_state['encryption_password'] if st.session_state['is_password_valid'] else ""
            histories = firestore_utils.get_history_list(user_uid, decryption_pass)

            if histories:
                hist_titles = {h["title"]: h for h in histories}
                history_select_key = f"history_select_{st.session_state.get('history_ui_key_counter', 0)}"

                selected_title = st.selectbox(
                    "履歴を選択", 
                    ["--- 選択してください ---"] + list(hist_titles.keys()),
                    key=history_select_key
                )
            else:
                st.caption("保存された履歴はありません。")

            # コールバックの結果（成功/エラーメッセージ）を表示
            if 'sidebar_msg' in st.session_state:
                msg_type, msg_text = st.session_state.pop('sidebar_msg')
                if msg_type == "success":
                    st.success(msg_text)
                else:
                    st.error(msg_text)
        else:
            st.caption("※ユーザー認証情報の取得に失敗しました。")

        st.divider()

        # --- History (ローカル用ダウンロード/アップロード) ---
        st.subheader("ローカル保存・復元")

        download_dict = session_state_manager.build_snapshot_from_session()
        download_data = json.dumps(download_dict, ensure_ascii=False, indent=2)

        dl_filename = st.session_state.get('chat_title') or f"chat_history_{time.strftime('%y%m%d_%H%M%S')}.json"
        if not dl_filename.endswith('.json'): dl_filename += '.json'

        st.download_button(
            label="現在の会話をJSONでダウンロード",
            data=download_data, file_name=dl_filename, mime="application/json", use_container_width=True
        )

        hist_up_key = f"hist_up_{st.session_state.get('history_ui_key_counter', 0)}"
        st.file_uploader(
            "JSONから会話を再開", 
            type="json", 
            key=hist_up_key, 
            on_change=load_history, 
            args=(hist_up_key,)
        )
        st.divider()

        st.header("ファイルを添付")

        if "uploaded_file_queue" not in st.session_state: st.session_state["uploaded_file_queue"] = []
        if "clipboard_queue" not in st.session_state: st.session_state["clipboard_queue"] = []
        if "uploader_key_counter" not in st.session_state: st.session_state["uploader_key_counter"] = 0
        if "last_pasted_hash" not in st.session_state: st.session_state["last_pasted_hash"] = None

        if st.session_state.get('clear_uploader', False):
            st.session_state["uploader_key_counter"] += 1
            st.session_state['clear_uploader'] = False

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

        st.subheader("分析・実行オプション")

        st.checkbox(
            label="📈 グラフ描画・データ分析 (Python自動実行)", 
            help="ONにすると、AIが生成したPythonコードをサーバー上で実行し、結果をチャットに返します。\nアップロードファイルは `files['ファイル名']` でアクセス可能です。",
            key="auto_plot_enabled" 
        )

        st.divider()

        st.subheader(config.UITexts.EDITOR_SUBHEADER)

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