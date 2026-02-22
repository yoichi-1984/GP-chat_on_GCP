import os
import json
import sys
import time
import traceback

import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from streamlit_ace import st_ace

# --- Import Logic ---
try:
    from gp_chat import config
    from gp_chat import utils
    from gp_chat import sidebar
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

    # 修正: ここにあった check_password 関数と呼び出し処理を削除しました

    st.title(config.UITexts.APP_TITLE)

    if "debug_logs" not in st.session_state: st.session_state["debug_logs"] = []

    # 必要なモジュールの読み込み
    PROMPTS = utils.load_prompts()
    APP_CONFIG = utils.load_app_config()
    supported_extensions = APP_CONFIG.get("file_uploader", {}).get("supported_extensions", [])

    # --- 【重要】Cloud Run対応の環境チェック ---
    env_files = utils.find_env_files()
    is_cloud_env = os.getenv("GCP_PROJECT_ID") is not None

    # .envがなく、かつCloud環境変数もない場合のみエラーにする
    if not env_files and not is_cloud_env:
        st.error("設定エラー: .env ファイルが見つからず、環境変数も設定されていません。")
        st.stop()

    # Session State Init
    for key, value in config.SESSION_STATE_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value.copy() if isinstance(value, (dict, list)) else value

    # Sidebar Render
    sidebar.render_sidebar(
        supported_extensions, env_files, load_history, 
        lambda i: st.session_state['python_canvases'].__setitem__(i, config.ACE_EDITOR_DEFAULT_CODE),
        lambda i, m: (st.session_state['messages'].append({"role": "user", "content": config.UITexts.REVIEW_PROMPT_MULTI.format(i=i+1) if m else config.UITexts.REVIEW_PROMPT_SINGLE}), st.session_state.__setitem__('is_generating', True)),
        lambda i: utils.run_pylint_validation(st.session_state['python_canvases'][i], i, PROMPTS),
        lambda i, k: st.session_state['python_canvases'].__setitem__(i, st.session_state[k].getvalue().decode("utf-8")) if st.session_state.get(k) else None
    )

    # --- Environment Loading ---
    if env_files:
        load_dotenv(dotenv_path=st.session_state.get('selected_env_file', env_files[0]), override=True)

    project_id = os.getenv(config.GCP_PROJECT_ID_NAME)
    location = os.getenv(config.GCP_LOCATION_NAME, "us-central1") 
    model_id = st.session_state.get('current_model_id', os.getenv(config.GEMINI_MODEL_ID_NAME, "gemini-3.1-pro-preview"))

    INPUT_LIMIT = 1000000
    OUTPUT_LIMIT = 65536
    max_tokens_val = min(int(os.getenv("MAX_TOKEN", "65536")), OUTPUT_LIMIT)

    # Client Init
    try:
        client = genai.Client(vertexai=True, project=project_id, location=location)
    except Exception as e:
        st.error(f"Client init error: {e}")
        st.caption("Check your GCP_PROJECT_ID and Region settings.")
        st.stop()

    st.caption(f"Backend: {model_id} | Location: {location}")

    # Debug Logs
    with st.expander("🛠 システムログ", expanded=False):
        for log in reversed(st.session_state["debug_logs"]):
            st.text(log)

    # System Prompt Setup
    if not st.session_state['system_role_defined']:
        st.subheader("AIの役割を設定")
        role = st.text_area("System Role", value=PROMPTS.get("system", {}).get("text", ""), height=200)
        if st.button("チャットを開始", type="primary"):
            st.session_state['messages'] = [{"role": "system", "content": role}]
            st.session_state['system_role_defined'] = True
            st.rerun()
        st.stop()

    # Chat History
    for msg in st.session_state['messages']:
        if msg["role"] != "system":
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if "grounding_metadata" in msg and msg["grounding_metadata"]:
                    with st.expander("🔎 検索ソース (Grounding)"):
                        st.json(msg["grounding_metadata"])
                if msg["role"] == "assistant" and "usage" in msg:
                    u = msg["usage"]
                    st.caption(f"Tokens: In {u['input_tokens']:,} / Out {u['output_tokens']:,}")

    # Input Area
    if prompt := st.chat_input("指示を入力...", disabled=st.session_state['is_generating']):
        st.session_state['messages'].append({"role": "user", "content": prompt})
        st.session_state['is_generating'] = True
        st.rerun()

    # Generation Logic
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

            if not is_special:
                if st.session_state.get('uploaded_file_queue'):
                    parts, _ = utils.process_uploaded_files_for_gemini(st.session_state['uploaded_file_queue'])
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

                final_grounding = {}
                if grounding_chunks:
                    last = grounding_chunks[-1]
                    if last.grounding_chunks:
                        srcs = [{"title": g.web.title, "uri": g.web.uri} for g in last.grounding_chunks if g.web]
                        if srcs: final_grounding["sources"] = srcs
                    if last.web_search_queries: final_grounding["queries"] = last.web_search_queries

                u_dict = {"total_tokens": usage_meta.total_token_count, "input_tokens": usage_meta.prompt_token_count, "output_tokens": usage_meta.candidates_token_count} if usage_meta else None
                if u_dict:
                    st.session_state['total_usage']['total_tokens'] += u_dict['total_tokens']

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
                st.rerun()

if __name__ == "__main__":
    run_chatbot_app()