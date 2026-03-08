import os
import json
import sys
import time
import traceback
import re
import tempfile
import base64
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from streamlit_ace import st_ace

try:
    from gp_chat import config
    from gp_chat import utils
    from gp_chat import sidebar
    from gp_chat import firestore_utils
    from gp_chat import execution_engine
except ImportError as e:
    st.error(f"Critical System Error: Failed to import application modules. {e}")
    st.stop()

# --- Helper Functions ---
def add_debug_log(message, level="info"):
    if "debug_logs" not in st.session_state:
        st.session_state["debug_logs"] = []
    timestamp = time.strftime("%H:%M:%S")
    st.session_state["debug_logs"].append(f"[{timestamp}] [{level.upper()}] {message}")
    if len(st.session_state["debug_logs"]) > 50:
        st.session_state["debug_logs"].pop(0)

def load_history(uploader_key):
    uploaded_file = st.session_state.get(uploader_key)
    if not uploaded_file: return
    try:
        loaded_data = json.load(uploaded_file)
        if isinstance(loaded_data, dict) and "messages" in loaded_data:
            st.session_state['messages'] = loaded_data["messages"]
            if "python_canvases" in loaded_data:
                st.session_state['python_canvases'] = loaded_data["python_canvases"]
            if "multi_code_enabled" in loaded_data:
                st.session_state['multi_code_enabled'] = loaded_data["multi_code_enabled"]
            st.success(config.UITexts.HISTORY_LOADED_SUCCESS)
            st.session_state['system_role_defined'] = True
            st.session_state['canvas_key_counter'] += 1
            add_debug_log("Session restored from JSON.")
    except Exception as e:
        st.error(f"Load failed: {e}")

# --- Streamlit Application ---
def run_chatbot_app():
    st.set_page_config(page_title=config.UITexts.APP_TITLE, layout="wide")
    st.title(config.UITexts.APP_TITLE)

    if "debug_logs" not in st.session_state: st.session_state["debug_logs"] = []

    user_uid = "unknown"
    try:
        if hasattr(st, "context"):
            headers = st.context.headers
            if "X-User-UID" in headers and headers["X-User-UID"] != "unknown":
                user_uid = headers["X-User-UID"]
            
            cookies = st.context.cookies
            if user_uid == "unknown" and "session" in cookies:
                from firebase_admin import auth
                session_cookie = cookies["session"]
                decoded_claims = auth.verify_session_cookie(session_cookie, check_revoked=False)
                user_uid = decoded_claims.get("uid", "unknown")
    except Exception as e:
        add_debug_log(f"UID fetch error: {e}", "error")

    PROMPTS = utils.load_prompts()
    APP_CONFIG = utils.load_app_config()
    supported_extensions = APP_CONFIG.get("file_uploader", {}).get("supported_extensions", [])
    env_files = utils.find_env_files()
    is_cloud_env = os.getenv("GCP_PROJECT_ID") is not None

    if not env_files and not is_cloud_env:
        st.error("設定エラー: .env ファイルが見つからず、環境変数も設定されていません。")
        st.stop()

    for key, value in config.SESSION_STATE_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value.copy() if isinstance(value, (dict, list)) else value

    if "chat_title" not in st.session_state: 
        st.session_state["chat_title"] = None

    sidebar.render_sidebar(
        supported_extensions, env_files, load_history, 
        lambda i: st.session_state['python_canvases'].__setitem__(i, config.ACE_EDITOR_DEFAULT_CODE),
        lambda i, m: (st.session_state['messages'].append({"role": "user", "content": config.UITexts.REVIEW_PROMPT_MULTI.format(i=i+1) if m else config.UITexts.REVIEW_PROMPT_SINGLE}), st.session_state.__setitem__('is_generating', True)),
        lambda i: utils.run_pylint_validation(st.session_state['python_canvases'][i], i, PROMPTS),
        lambda i, k: st.session_state['python_canvases'].__setitem__(i, st.session_state[k].getvalue().decode("utf-8")) if st.session_state.get(k) else None,
        user_uid=user_uid
    )

    if env_files:
        load_dotenv(dotenv_path=st.session_state.get('selected_env_file', env_files[0]), override=True)

    project_id = os.getenv(config.GCP_PROJECT_ID_NAME)
    location = os.getenv(config.GCP_LOCATION_NAME, "us-central1") 
    model_id = st.session_state.get('current_model_id', os.getenv(config.GEMINI_MODEL_ID_NAME, "gemini-3.1-pro-preview"))

    INPUT_LIMIT = 1000000
    OUTPUT_LIMIT = 65536
    max_tokens_val = min(int(os.getenv("MAX_TOKEN", "65536")), OUTPUT_LIMIT)

    try:
        client = genai.Client(vertexai=True, project=project_id, location=location)
    except Exception as e:
        st.error(f"Client init error: {e}")
        st.stop()

    with st.expander("🛠 システムログ", expanded=False):
        for log in reversed(st.session_state["debug_logs"]):
            st.text(log)

    if not st.session_state['system_role_defined']:
        st.subheader("AIの役割を設定")
        role = st.text_area("System Role", value=PROMPTS.get("system", {}).get("text", "あなたは優秀なデータサイエンティストです。データ分析やグラフ作成が必要な場合は、Pythonコードを生成してください。システムが自動で実行します。"), height=200)
        if st.button("チャットを開始", type="primary"):
            st.session_state['messages'] = [{"role": "system", "content": role}]
            st.session_state['system_role_defined'] = True
            st.rerun()
        st.stop()

    for msg in st.session_state['messages']:
        if msg["role"] != "system":
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"], unsafe_allow_html=True)
                if "grounding_metadata" in msg and msg["grounding_metadata"]:
                    with st.expander("🔎 検索ソース (Grounding)"):
                        st.json(msg["grounding_metadata"])
                if msg["role"] == "assistant" and "usage" in msg:
                    u = msg["usage"]
                    st.caption(f"Tokens: In {u['input_tokens']:,} / Out {u['output_tokens']:,}")

    if prompt := st.chat_input("指示を入力...", disabled=st.session_state.get('is_generating', False)):
        st.session_state['messages'].append({"role": "user", "content": prompt})
        st.session_state['is_generating'] = True
        st.rerun()

    # --- Generation Logic ---
    if st.session_state['is_generating']:
        with st.chat_message("assistant"):
            thought_area = st.empty()
            with thought_area.container():
                thought_status = st.status("Thinking...", expanded=False)
                thought_ph = thought_status.empty()

            text_ph = st.empty()
            full_res = ""
            full_thought = ""
            grounding_chunks = []
            usage_meta = None

            is_special = 'special_generation_messages' in st.session_state and st.session_state['special_generation_messages']
            msgs = st.session_state['special_generation_messages'] if is_special else st.session_state['messages']

            contents = []
            sys_inst = ""
            for m in msgs:
                if m["role"] == "system": sys_inst = m["content"]
                else: contents.append(types.Content(role=m["role"], parts=[types.Part.from_text(text=m["content"])]))

            # --- ★追加: データ分析モードON時のシステムプロンプト動的追加 ---
            if st.session_state.get('auto_plot_enabled', False) and not is_special:
                sys_inst += "\n\n【システム設定】現在「データ分析モード(Python自動実行)」がONです。ユーザーの要求に応じて、データ分析やグラフ作成が必要な場合はPythonコードを生成してください。コードは必ず ```python と ``` で囲んでください。アップロードされたファイルへのパスは辞書 `files` から `files['ファイル名']` で取得可能です。"

            # アップロードキューの処理
            combined_queue = st.session_state.get('uploaded_file_queue', []) + st.session_state.get('clipboard_queue', [])
            if not is_special:
                if combined_queue:
                    parts, _ = utils.process_uploaded_files_for_gemini(combined_queue)
                    if parts and contents: contents[-1].parts = parts + contents[-1].parts

                c_parts = []
                for i, c in enumerate(st.session_state['python_canvases']):
                    if c.strip() and c != config.ACE_EDITOR_DEFAULT_CODE:
                        c_parts.append(types.Part.from_text(text=f"\n[Canvas-{i+1}]\n```python\n{c}\n```"))
                if c_parts and contents: contents[-1].parts = c_parts + contents[-1].parts

            effort = st.session_state.get('reasoning_effort', 'high')
            t_lvl = types.ThinkingLevel.HIGH if effort == 'high' else types.ThinkingLevel.LOW
            tools = [types.Tool(google_search=types.GoogleSearch())] if st.session_state.get('enable_google_search') and not is_special else []

            try:
                cfg = types.GenerateContentConfig(system_instruction=sys_inst, max_output_tokens=max_tokens_val, tools=tools)
                if "gemini-3" in model_id:
                    cfg.thinking_config = types.ThinkingConfig(thinking_level=t_lvl, include_thoughts=True)

                stream = client.models.generate_content_stream(model=model_id, contents=contents, config=cfg)

                for chunk in stream:
                    if chunk.usage_metadata: usage_meta = chunk.usage_metadata
                    if not chunk.candidates: continue
                    cand = chunk.candidates[0]

                    if cand.grounding_metadata:
                        grounding_chunks.append(cand.grounding_metadata)
                        if cand.grounding_metadata.web_search_queries:
                            for q in cand.grounding_metadata.web_search_queries:
                                full_thought += f"\n\n🔍 Search: `{q}`\n\n"
                                thought_ph.markdown(full_thought)

                    if cand.content and cand.content.parts:
                        for part in cand.content.parts:
                            is_thought = False
                            txt = ""
                            if hasattr(part, 'thought') and part.thought:
                                is_thought = True
                                txt = part.thought if isinstance(part.thought, str) else part.text

                            if is_thought:
                                full_thought += txt
                                thought_ph.markdown(full_thought)
                            elif part.text:
                                full_res += part.text
                                text_ph.markdown(full_res + "▌")

                text_ph.markdown(full_res)
                if not full_thought: thought_area.empty()
                else: thought_status.update(label="Thinking Complete", state="complete")

                # --- ★追加: AIが生成したコードの自律実行 (チェックON時のみ) ---
                if st.session_state.get('auto_plot_enabled', False):
                    code_blocks = re.findall(r'```python\n(.*?)```', full_res, re.DOTALL)
                    
                    if code_blocks:
                        st.info("💡 Pythonコードを検知しました。自律実行を開始します...")
                        code_to_run = code_blocks[-1] # 最後に生成されたコードを実行
                        
                        tmp_files = []
                        file_paths_dict = {}
                        
                        try:
                            # 1. アップロードされたファイルを /tmp に書き出し
                            for vf in combined_queue:
                                ext = os.path.splitext(vf.name)[1]
                                fd, path = tempfile.mkstemp(suffix=ext)
                                with os.fdopen(fd, 'wb') as f:
                                    f.write(vf.getvalue())
                                file_paths_dict[vf.name] = path
                                tmp_files.append(path)
                            
                            # 2. コード実行エンジン呼び出し
                            with st.spinner("Pythonコードを実行中..."):
                                stdout_str, figures = execution_engine.execute_user_code(
                                    code=code_to_run, 
                                    file_paths=file_paths_dict, 
                                    canvases=st.session_state.get('python_canvases', [])
                                )
                            
                            # 3. 実行結果の構築
                            exec_res_md = f"**▶️ システム実行結果:**\n```text\n{stdout_str.strip() or 'No text output'}\n```"
                            
                            for fig_buf in figures:
                                b64_str = base64.b64encode(fig_buf.getvalue()).decode()
                                exec_res_md += f"\n\n<img src='data:image/png;base64,{b64_str}' width='600'>"
                            
                            st.markdown(exec_res_md, unsafe_allow_html=True)
                            full_res += f"\n\n---\n{exec_res_md}"
                            
                        except Exception as e:
                            err_msg = f"**▶️ システム実行エラー:**\n```text\n{e}\n```"
                            st.error(err_msg)
                            full_res += f"\n\n---\n{err_msg}"
                        finally:
                            # 4. /tmp の一時ファイルを確実にお掃除
                            for path in tmp_files:
                                try: os.remove(path)
                                except: pass

                # メタデータと最終結果の保存
                final_grounding = {}
                if grounding_chunks:
                    last = grounding_chunks[-1]
                    if last.grounding_chunks:
                        srcs = [{"title": g.web.title, "uri": g.web.uri} for g in last.grounding_chunks if g.web]
                        if srcs: final_grounding["sources"] = srcs
                    if last.web_search_queries: final_grounding["queries"] = last.web_search_queries

                u_dict = {"total_tokens": usage_meta.total_token_count, "input_tokens": usage_meta.prompt_token_count, "output_tokens": usage_meta.candidates_token_count} if usage_meta else None
                
                as_msg = {"role": "assistant", "content": full_res}
                if u_dict: as_msg["usage"] = u_dict
                if final_grounding: as_msg["grounding_metadata"] = final_grounding

                if is_special:
                    for m in msgs:
                        if m["role"] == "user": st.session_state['messages'].append(m)
                    del st.session_state['special_generation_messages']

                st.session_state['messages'].append(as_msg)

            except Exception as e:
                st.error(f"Generation Error: {e}")
            finally:
                st.session_state['is_generating'] = False

        # --- タイトル自動生成＆Firestore保存 ---
        user_msgs = [m['content'] for m in st.session_state['messages'] if m["role"] == "user"]

        if len(user_msgs) >= 1 and not st.session_state.get('chat_title'):
            try:
                title_prompt = f"以下のユーザー発言を要約し、チャットのファイル名となるタイトルを20文字以内で生成してください。結果の文字列のみ出力し、改行や記号は含めないでください。\n{user_msgs[0]}"
                title_res = client.models.generate_content(model=model_id, contents=title_prompt)
                date_str = datetime.now().strftime("%y%m%d")
                clean_title = title_res.text.strip().replace("/", "_").replace("\\", "_")
                st.session_state['chat_title'] = f"{date_str}_{clean_title}.json"
            except Exception as e:
                st.session_state['chat_title'] = f"{datetime.now().strftime('%y%m%d')}_新規チャット.json"

        if st.session_state.get('chat_title'):
            firestore_utils.save_chat_to_firestore(
                uid=user_uid,
                chat_title=st.session_state['chat_title'],
                messages=st.session_state['messages'],
                is_encrypted=st.session_state.get('use_encryption', False),
                password=st.session_state.get('encryption_password', "")
            )
        
        st.rerun()

if __name__ == "__main__":
    run_chatbot_app()