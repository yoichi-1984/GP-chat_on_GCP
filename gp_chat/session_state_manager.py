# gp_chat/session_state_manager.py
import streamlit as st
import copy
from . import config

def reset_conversation_state():
    """設定(PREFERENCE_KEYS)は保持し、会話とUI状態をデフォルトに戻す"""
    next_canvas_key = st.session_state.get('canvas_key_counter', 0) + 1
    next_uploader_key = st.session_state.get('uploader_key_counter', 0) + 1
    next_history_ui_key = st.session_state.get('history_ui_key_counter', 0) + 1

    for k in config.CONVERSATION_KEYS + config.UI_EPHEMERAL_KEYS:
        if k in config.SESSION_STATE_DEFAULTS:
            st.session_state[k] = copy.deepcopy(config.SESSION_STATE_DEFAULTS[k])
        elif k in st.session_state:
            st.session_state.pop(k)

    # カウンターを新しい値で上書きしてWidgetを強制再生成
    st.session_state['canvas_key_counter'] = next_canvas_key
    st.session_state['uploader_key_counter'] = next_uploader_key
    st.session_state['history_ui_key_counter'] = next_history_ui_key
    st.session_state['clear_uploader'] = True

def build_snapshot_from_session() -> dict:
    """保存用のデータをセッションから抽出する"""
    return {
        key: copy.deepcopy(st.session_state[key])
        for key in config.SNAPSHOT_KEYS
        if key in st.session_state
    }

def normalize_snapshot(raw: dict) -> dict:
    """古い/壊れたデータを正規化する（安全装置）"""
    if not isinstance(raw, dict) or "messages" not in raw:
        raise ValueError("Invalid snapshot format: 'messages' key missing.")

    messages = raw.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("'messages' must be a list.")

    normalized_messages = []
    has_system = False

    for msg in messages:
        if isinstance(msg, dict) and "role" in msg and "content" in msg:
            if msg["role"] in config.VALID_MESSAGE_ROLES:
                normalized_messages.append(msg)
                if msg["role"] == "system":
                    has_system = True

    # 古い履歴でsystemロールがない場合は先頭に補完してクラッシュを防ぐ
    if not has_system:
        normalized_messages.insert(0, {"role": "system", "content": config.DEFAULT_SYSTEM_ROLE})

    raw["messages"] = normalized_messages
    raw["system_role_defined"] = True 

    canvases = raw.get("python_canvases", [])
    if not isinstance(canvases, list) or len(canvases) == 0:
        raw["python_canvases"] = [config.ACE_EDITOR_DEFAULT_CODE]
    else:
        raw["python_canvases"] = [str(c) if c else config.ACE_EDITOR_DEFAULT_CODE for c in canvases]

    raw["multi_code_enabled"] = len(raw["python_canvases"]) > 1

    if raw.get("current_model_id") not in config.AVAILABLE_MODELS:
        raw["current_model_id"] = config.DEFAULT_MODEL_ID

    if raw.get("reasoning_effort") not in config.VALID_REASONING_EFFORTS:
        raw["reasoning_effort"] = "high"

    return raw

def apply_snapshot_to_session(snapshot: dict):
    """スナップショットをセッションに適用し、UI一時状態をリセットする"""
    # 1. 復元対象となる会話状態を一旦デフォルトに戻す
    for key in config.CONVERSATION_KEYS:
        if key in config.SESSION_STATE_DEFAULTS:
            st.session_state[key] = copy.deepcopy(config.SESSION_STATE_DEFAULTS[key])

    # 2. スナップショットの値を適用
    for key, value in snapshot.items():
        if key in config.SNAPSHOT_KEYS:
            st.session_state[key] = copy.deepcopy(value)

    # 3. UI一時状態の確実なクリアとカウンター更新
    st.session_state['uploaded_file_queue'] = []
    st.session_state['clipboard_queue'] = []
    st.session_state['debug_logs'] = []
    st.session_state['is_generating'] = False
    st.session_state['clear_uploader'] = True
    st.session_state['last_pasted_hash'] = None
    st.session_state['canvas_key_counter'] = st.session_state.get('canvas_key_counter', 0) + 1
    st.session_state['history_ui_key_counter'] = st.session_state.get('history_ui_key_counter', 0) + 1
    st.session_state['uploader_key_counter'] = st.session_state.get('uploader_key_counter', 0) + 1
    st.session_state.pop('special_generation_messages', None)

def restore_snapshot(raw: dict):
    """normalize と apply を連続実行する公開API"""
    normalized_data = normalize_snapshot(raw)
    apply_snapshot_to_session(normalized_data)