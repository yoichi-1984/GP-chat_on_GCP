import streamlit as st
import os
import json
import time
from streamlit_ace import st_ace
from . import config
from . import firestore_utils

def render_sidebar(supported_types, env_files, load_history, handle_clear, handle_review, handle_validation, handle_file_upload, user_uid="unknown"):
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

        # --- 修正: コールバック関数を使用してリセット処理を行う ---
        def reset_conversation():
            for k, v in config.SESSION_STATE_DEFAULTS.items(): 
                st.session_state[k] = v
            st.session_state['chat_title'] = None

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
            value=st.session_state['encryption_password'], 
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

        # --- 過去のチャット履歴 ---
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
                    chat_json_str = raw_data.get("chat_data", "{}")
                    
                    if raw_data.get("is_encrypted"):
                        if not st.session_state['is_password_valid']:
                            st.error("暗号化されています。正しい暗号化キーを入力してください。")
                            st.stop()
                        chat_json_str = firestore_utils.decrypt_text(chat_json_str, st.session_state['encryption_password'])
                    
                    try:
                        loaded_data = json.loads(chat_json_str)
                        if "messages" in loaded_data:
                            st.session_state['messages'] = loaded_data["messages"]
                            st.session_state['chat_title'] = raw_data.get("display_title", selected_title)
                            # ★修正: 履歴読み込み時は、システムロール設定画面をスキップする
                            st.session_state['system_role_defined'] = True
                            st.success("履歴を復元しました！")
                            st.rerun()
                    except json.JSONDecodeError:
                        st.error("復号に失敗しました。データが破損しています。")
            else:
                st.caption("保存された履歴はありません。")
        else:
            st.caption("※ユーザー認証情報の取得に失敗しました。")

        st.divider()

        # --- History (ローカル用ダウンロード/アップロード) ---
        st.subheader("ローカル保存・復元")
        
        # ダウンロード用のJSON文字列を作成
        download_data = json.dumps({
            "messages": st.session_state.get('messages', []),
            "python_canvases": st.session_state.get('python_canvases', []),
            "multi_code_enabled": st.session_state.get('multi_code_enabled', False)
        }, ensure_ascii=False, indent=2)
        
        # ダウンロードファイル名（chat_titleがあればそれを使用、なければ日付ベース）
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
        if "uploaded_file_queue" not in st.session_state: st.session_state["uploaded_file_queue"] = []
        uf = st.file_uploader("アップロード", type=["png","pdf","docx","pptx","py","txt","bat"], accept_multiple_files=True, key="main_up")
        if uf: st.session_state["uploaded_file_queue"] = uf
        else: st.session_state["uploaded_file_queue"] = []

        st.divider()
        st.subheader(config.UITexts.EDITOR_SUBHEADER)
        multi_code_enabled = st.checkbox(config.UITexts.MULTI_CODE_CHECKBOX, value=st.session_state.get('multi_code_enabled', False))
        if multi_code_enabled != st.session_state.get('multi_code_enabled', False):
            st.session_state['multi_code_enabled'] = multi_code_enabled
            st.rerun()

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
                
                c1, c2, c3 = st.columns(3)
                c1.button("クリア", key=f"clr_{i}", on_click=handle_clear, args=(i,), use_container_width=True)
                c2.button("レビュー", key=f"rev_{i}", on_click=handle_review, args=(i, True), use_container_width=True)
                c3.button("検証", key=f"val_{i}", on_click=handle_validation, args=(i,), use_container_width=True)

                up_key = f"up_{i}_{st.session_state['canvas_key_counter']}"
                st.file_uploader(f"Load into Canvas-{i+1}", type=supported_types, key=up_key, on_change=handle_file_upload, args=(i, up_key))
                st.divider()
        else:
            if len(canvases) > 1:
                st.session_state['python_canvases'] = [canvases[0]]
                st.rerun()
            
            ace_key = f"ace_single_{st.session_state['canvas_key_counter']}"
            updated = st_ace(value=canvases[0], key=ace_key, **config.ACE_EDITOR_SETTINGS, auto_update=True)
            if updated != canvases[0]:
                canvases[0] = updated

            c1, c2, c3 = st.columns(3)
            c1.button("Clear", key="clr_s", on_click=handle_clear, args=(0,), use_container_width=True)
            c2.button("Review", key="rev_s", on_click=handle_review, args=(0, False), use_container_width=True)
            c3.button("Validate", key="val_s", on_click=handle_validation, args=(0,), use_container_width=True)
            
            up_key = f"up_s_{st.session_state['canvas_key_counter']}"
            st.file_uploader("Load into Canvas", type=supported_types, key=up_key, on_change=handle_file_upload, args=(0, up_key))
            
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
        