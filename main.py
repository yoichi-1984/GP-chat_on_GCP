import base64
import copy
import json
import os
import random
import re
import time
import uuid
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components  # JS埋め込み用
from dotenv import load_dotenv
from google import genai
from google.genai import types

try:
    from gp_chat import cloud_logging_utils
    from gp_chat import config
    from gp_chat import firestore_utils
    from gp_chat import session_state_manager
    from gp_chat import sidebar
    from gp_chat import utils
except ImportError as e:
    st.error(f"重大なシステムエラー: アプリケーションモジュールの読み込みに失敗しました。{e}")
    st.stop()


def add_debug_log(message, level="info"):
    if "debug_logs" not in st.session_state:
        st.session_state["debug_logs"] = []
    timestamp = time.strftime("%H:%M:%S")
    st.session_state["debug_logs"].append(f"[{timestamp}] [{level.upper()}] {message}")
    if len(st.session_state["debug_logs"]) > 50:
        st.session_state["debug_logs"].pop(0)


def bump_chat_revision():
    st.session_state["chat_revision"] = st.session_state.get("chat_revision", 0) + 1


def _clear_exec_retry_state():
    st.session_state["exec_retry_state"] = None
    st.session_state["pending_exec_message"] = None


def _build_initial_exec_retry_state(
    candidate_message,
    last_code,
    file_payloads,
    canvases,
    usage_totals=None,
    initial_grounding_metadata=None,
    max_attempts=2,
):
    return {
        "attempt_index": 0,
        "max_attempts": max(0, int(max_attempts)),
        "candidate_message": copy.deepcopy(candidate_message) if candidate_message is not None else None,
        "last_code": last_code,
        "file_payloads": copy.deepcopy(file_payloads or {}),
        "canvases": copy.deepcopy(canvases or []),
        "usage_totals": copy.deepcopy(usage_totals or {}),
        "initial_grounding_metadata": copy.deepcopy(initial_grounding_metadata),
        "last_execution": None,
    }


def _append_final_exec_message(candidate_message, execution_result_md):
    final_message = copy.deepcopy(candidate_message) if candidate_message is not None else {"role": "assistant", "content": ""}
    existing_content = str(final_message.get("content", ""))
    appended_content = str(execution_result_md or "")
    if appended_content:
        separator = "\n\n---\n" if existing_content else ""
        final_message["content"] = f"{existing_content}{separator}{appended_content}"
    else:
        final_message["content"] = existing_content
    return final_message


def _normalize_usage_dict(raw_usage):
    if not isinstance(raw_usage, dict):
        return {}

    normalized = {}
    for key in ("total_tokens", "input_tokens", "output_tokens"):
        value = raw_usage.get(key)
        if value is None:
            continue
        try:
            normalized[key] = int(value)
        except (TypeError, ValueError):
            continue
    return normalized


def _usage_dict_from_usage_metadata(usage_meta):
    if not usage_meta:
        return {}

    input_tokens = getattr(usage_meta, "prompt_token_count", None)
    output_tokens = getattr(usage_meta, "candidates_token_count", None)
    if input_tokens is None or output_tokens is None:
        return {}

    return _normalize_usage_dict(
        {
            "total_tokens": getattr(usage_meta, "total_token_count", None),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    )


def _merge_usage_totals(base_usage, additional_usage):
    normalized_base = _normalize_usage_dict(base_usage)
    normalized_additional = _normalize_usage_dict(additional_usage)
    if not normalized_base and not normalized_additional:
        return {}

    merged = {}
    for key in ("total_tokens", "input_tokens", "output_tokens"):
        value = normalized_base.get(key, 0) + normalized_additional.get(key, 0)
        if value:
            merged[key] = value
    return merged


def _cloud_logging_token_usage(usage):
    normalized_usage = _normalize_usage_dict(usage)
    return cloud_logging_utils.build_token_usage(normalized_usage)


def _emit_ai_usage_log(user_email, task_id, model_id, usage, additional_info=None):
    cloud_logging_utils.log_ai_usage(
        app_name=cloud_logging_utils.resolve_app_name(),
        user_email=user_email,
        task_id=task_id,
        model_name=model_id,
        token_usage=_cloud_logging_token_usage(usage),
        additional_info=additional_info,
    )


def _collect_current_file_payloads():
    combined_queue = st.session_state.get("uploaded_file_queue", []) + st.session_state.get("clipboard_queue", [])
    file_payloads = {}
    for vf in combined_queue:
        safe_name = os.path.basename(vf.name)
        if not safe_name:
            continue
        file_payloads[safe_name] = vf.getvalue()
    return file_payloads


def _clear_pending_execution_buffers():
    st.session_state["pending_exec_code"] = None
    st.session_state["clipboard_queue"] = []


def _next_exec_post_action():
    return "generate_title" if should_generate_title() else "save"


def _extract_latest_python_block(text):
    if not text:
        return None
    code_blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    if not code_blocks:
        return None
    return code_blocks[-1]


def _build_execution_result_stdout(execution_result):
    debug_suffix = f"\n{execution_result.debug_log}" if execution_result.debug_log else ""
    stdout_text = (execution_result.stdout or "").rstrip()
    error_message = (execution_result.error_message or "").strip()
    traceback_text = (execution_result.traceback or "").rstrip()

    if execution_result.status == "ok":
        return stdout_text

    if execution_result.status == "runtime_error":
        details = []
        if stdout_text:
            details.append(stdout_text)
        if traceback_text and traceback_text not in details:
            details.append(traceback_text)
        if not details and error_message:
            details.append(error_message)
        if not details:
            details.append("Runtime error.")
        return "[Runtime Error]\n" + "\n".join(details) + debug_suffix

    if execution_result.status == "security_error":
        details = stdout_text or error_message or "Security policy violation."
        if details.startswith("[Security Error]"):
            return f"{details}{debug_suffix}"
        return f"[Security Error]\n{details}{debug_suffix}"

    if execution_result.status == "network_error":
        details = stdout_text or error_message or "External network access is disabled during code execution."
        if details.startswith("[Network Error]"):
            return f"{details}{debug_suffix}"
        return f"[Network Error]\n{details}{debug_suffix}"

    if execution_result.status == "startup_timeout":
        return f"[Startup Timeout]\n{error_message or 'Sandbox worker did not become ready.'}{debug_suffix}"

    if execution_result.status == "execution_timeout":
        return f"[Execution Timeout]\n{error_message or 'Code execution exceeded the time limit.'}{debug_suffix}"

    details = []
    if stdout_text:
        details.append(stdout_text)
    if error_message and error_message not in details:
        details.append(error_message)
    if traceback_text and traceback_text not in details:
        details.append(traceback_text)
    if not details:
        details.append("Sandbox worker returned a system error.")

    return "[System Error]\n" + "\n".join(details) + debug_suffix


def _build_execution_result_markdown(execution_result):
    stdout_str = _build_execution_result_stdout(execution_result)
    exec_res_md = f"**▶️ システム実行結果:**\n```text\n{stdout_str.strip() or 'テキスト出力はありません'}\n```"
    for fig_buf in execution_result.figures:
        b64_str = base64.b64encode(fig_buf.getvalue()).decode()
        exec_res_md += f"\n\n<img src='data:image/png;base64,{b64_str}' width='600'>"
    return exec_res_md


def _get_pending_exec_candidate_message():
    pending_message = st.session_state.get("pending_exec_message")
    if pending_message:
        return copy.deepcopy(pending_message)

    retry_state = st.session_state.get("exec_retry_state") or {}
    candidate_message = retry_state.get("candidate_message")
    if candidate_message:
        return copy.deepcopy(candidate_message)

    if st.session_state.get("messages") and st.session_state["messages"][-1]["role"] == "assistant":
        return copy.deepcopy(st.session_state["messages"][-1])

    return {"role": "assistant", "content": ""}


def _append_pending_exec_message_to_history(execution_result_md):
    candidate_message = _get_pending_exec_candidate_message()
    retry_state = st.session_state.get("exec_retry_state") or {}
    final_message = _append_final_exec_message(candidate_message, execution_result_md)

    usage_totals = _normalize_usage_dict(retry_state.get("usage_totals") or candidate_message.get("usage"))
    if usage_totals:
        final_message["usage"] = usage_totals
    else:
        final_message.pop("usage", None)

    initial_grounding = retry_state.get("initial_grounding_metadata")
    if initial_grounding:
        final_message["grounding_metadata"] = copy.deepcopy(initial_grounding)
    else:
        final_message.pop("grounding_metadata", None)

    has_pending_candidate = st.session_state.get("pending_exec_message") is not None or st.session_state.get("exec_retry_state") is not None
    if has_pending_candidate:
        st.session_state["messages"].append(final_message)
    elif st.session_state.get("messages") and st.session_state["messages"][-1]["role"] == "assistant":
        st.session_state["messages"][-1] = final_message
    else:
        st.session_state["messages"].append(final_message)
    bump_chat_revision()


def _execution_result_to_retry_state_payload(execution_result):
    return {
        "status": execution_result.status,
        "stdout": execution_result.stdout,
        "error_message": execution_result.error_message,
        "traceback": execution_result.traceback,
        "debug_log": execution_result.debug_log,
    }


def _should_retry_execution_result(execution_result, retry_state):
    if execution_result.status != "runtime_error":
        return False
    if not retry_state:
        return False
    return retry_state.get("attempt_index", 0) < retry_state.get("max_attempts", 0)


def _build_exec_retry_prompt(retry_state):
    last_execution = retry_state.get("last_execution") or {}
    file_names = sorted((retry_state.get("file_payloads") or {}).keys())
    canvases = retry_state.get("canvases") or []
    canvas_lines = []
    for index, canvas_code in enumerate(canvases, start=1):
        if not str(canvas_code or "").strip():
            continue
        canvas_lines.append(f"[Canvas-{index}]\n```python\n{canvas_code}\n```")

    available_files = ", ".join(file_names) if file_names else "なし"
    canvas_section = "\n\n".join(canvas_lines) if canvas_lines else "なし"
    attempt_number = max(1, retry_state.get("attempt_index", 0))
    max_attempts = retry_state.get("max_attempts", 0)

    return (
        "以下は、直前の assistant が生成した Python コードと、その実行結果です。"
        " 実行エラーの原因を踏まえてコードを修正し、修正版の Python コードだけを ```python ブロックで 1 つ返してください。"
        " 説明文や補足は不要です。Google 検索は使わず、利用可能なファイルと canvas 情報だけを使ってください。\n\n"
        f"Retry attempt {attempt_number} of {max_attempts}\n\n"
        "直前の assistant 応答:\n"
        f"{_sanitize_message_content_for_model(retry_state.get('candidate_message', {}).get('content', ''))}\n\n"
        "実行した Python コード:\n"
        f"```python\n{retry_state.get('last_code', '')}\n```\n\n"
        f"実行結果ステータス: {last_execution.get('status', '')}\n"
        f"error_message: {last_execution.get('error_message', '')}\n"
        f"stdout:\n{last_execution.get('stdout', '')}\n\n"
        f"traceback:\n{last_execution.get('traceback', '')}\n\n"
        f"利用可能な files: {available_files}\n\n"
        f"利用可能な canvas:\n{canvas_section}\n"
    )


def is_default_chat_title(title):
    if not title:
        return False
    normalized = str(title).strip()
    return bool(re.fullmatch(r"\d{6}_(新規チャット|Untitled)", normalized))


def should_generate_title():
    user_count = sum(1 for m in st.session_state["messages"] if m.get("role") == "user")
    assistant_count = sum(1 for m in st.session_state["messages"] if m.get("role") == "assistant")
    current_title = st.session_state.get("chat_title")
    has_real_title = bool(current_title) and not is_default_chat_title(current_title)
    title_generation_attempted = st.session_state.get("title_generation_attempted", False)
    if is_default_chat_title(current_title):
        title_generation_attempted = False
    return (
        not has_real_title
        and not title_generation_attempted
        and user_count >= 2
        and assistant_count >= 2
    )


def _sanitize_message_content_for_model(content):
    text = str(content or "")
    text = re.sub(
        r"<img\s+src=['\"]data:image/[^'\"]+['\"][^>]*>",
        "\n[生成画像は省略]\n",
        text,
        flags=re.IGNORECASE,
    )
    return text


def can_autosave(user_uid):
    if user_uid == "unknown":
        return False, "user uid unavailable"
    if not st.session_state.get("messages"):
        return False, "no messages"
    if st.session_state.get("chat_revision", 0) <= st.session_state.get("last_saved_revision", 0):
        return False, "already saved"
    return True, None


def build_title_source_text():
    lines = []
    for msg in st.session_state["messages"]:
        if msg["role"] not in ("user", "assistant"):
            continue
        prefix = "ユーザー" if msg["role"] == "user" else "AI"
        text = _sanitize_message_content_for_model(msg["content"]).replace("\n", " ").strip()
        lines.append(f"{prefix}: {text[:200]}")
        if len(lines) >= 4:
            break
    return "\n".join(lines)


def extract_response_text(response):
    text = getattr(response, "text", None)
    if text:
        return text

    chunks = []
    for cand in getattr(response, "candidates", []) or []:
        content = getattr(cand, "content", None)
        if not content:
            continue

        for part in getattr(content, "parts", []) or []:
            if getattr(part, "thought", False):
                continue
            part_text = getattr(part, "text", None)
            if part_text:
                chunks.append(part_text)

    return "".join(chunks)


def sanitize_generated_title(raw_title):
    if not raw_title:
        return ""

    clean_title = re.sub(r"\s+", " ", str(raw_title)).strip()
    clean_title = clean_title.strip("\"'「」")
    clean_title = clean_title.replace("/", "_").replace("\\", "_")
    return clean_title.strip()


def build_title_generation_config(model_id, enable_thinking=True):
    cfg = types.GenerateContentConfig(max_output_tokens=64)
    if enable_thinking and supports_thinking(model_id):
        cfg.thinking_config = types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW,
            include_thoughts=False,
        )
    return cfg


def summarize_title_response(response):
    if response is None:
        return "response=None"

    candidate_count = len(getattr(response, "candidates", []) or [])
    finish_reason = None
    part_kinds = []
    if candidate_count:
        first_candidate = response.candidates[0]
        finish_reason = getattr(first_candidate, "finish_reason", None)
        content = getattr(first_candidate, "content", None)
        if content and getattr(content, "parts", None):
            for part in content.parts:
                active_fields = [
                    field_name
                    for field_name, field_value in part.model_dump(
                        exclude={"text", "thought", "thought_signature"},
                        exclude_none=True,
                    ).items()
                    if field_value is not None
                ]
                if getattr(part, "thought", False):
                    active_fields.append("thought")
                if getattr(part, "text", None) is not None:
                    active_fields.append("text")
                part_kinds.append(active_fields or ["empty"])

    usage = getattr(response, "usage_metadata", None)
    output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
    thought_tokens = getattr(usage, "thoughts_token_count", None) if usage else None
    return (
        f"candidates={candidate_count}, finish_reason={finish_reason}, "
        f"parts={part_kinds}, output_tokens={output_tokens}, thought_tokens={thought_tokens}"
    )


def supports_thinking(model_id):
    return model_id.startswith(("gemini-2.5", "gemini-3"))


def get_error_code(exc):
    return getattr(exc, "code", None) or getattr(exc, "status_code", None)


def summarize_error(exc):
    return (
        f"type={type(exc).__name__}, "
        f"code={get_error_code(exc)}, "
        f"status={getattr(exc, 'status', None)}, "
        f"detail={exc}"
    )


def is_rate_limited_error(exc):
    code = get_error_code(exc)
    if code == 429:
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in ("429", "resource exhausted", "resource_exhausted", "too many requests")
    )


def is_retryable_priority_error(exc):
    code = get_error_code(exc)
    if code in {408, 429, 500, 502, 503, 504}:
        return True
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "resource exhausted",
            "resource_exhausted",
            "too many requests",
            "timed out",
            "timeout",
            "internal error",
            "service unavailable",
            "bad gateway",
        )
    )


def wait_before_standard_retry(request_label, exc):
    wait_seconds = random.uniform(0.25, 0.75)
    add_debug_log(
        (
            f"{request_label}: standard lane throttled "
            f"({summarize_error(exc)}); retrying after {wait_seconds:.2f}s."
        ),
        "warning",
    )
    time.sleep(wait_seconds)


def wait_before_priority_retry(request_label, retry_number, exc):
    base_wait_seconds = (2.0, 4.0, 8.0)[min(retry_number - 1, 2)]
    wait_seconds = base_wait_seconds + random.uniform(0.0, 1.0)
    add_debug_log(
        (
            f"{request_label}: priority retry {retry_number}/3 after "
            f"{wait_seconds:.2f}s ({summarize_error(exc)})."
        ),
        "warning",
    )
    time.sleep(wait_seconds)


def build_vertex_client(project_id, location, priority=False):
    headers = {"X-Vertex-AI-LLM-Request-Type": "shared"}
    if priority:
        headers["X-Vertex-AI-LLM-Shared-Request-Type"] = "priority"

    return genai.Client(
        vertexai=True,
        project=project_id,
        location=location,
        http_options=types.HttpOptions(
            api_version="v1",
            headers=headers,
        ),
    )


def generate_content_with_fallback(
    standard_client,
    priority_client,
    model_id,
    contents,
    config=None,
    request_label="request",
    enable_standard_retry=True,
    allow_priority_fallback=True,
):
    clients = {"standard": standard_client, "priority": priority_client}
    last_exc = None

    def _generate_once(lane_name):
        add_debug_log(f"{request_label}: using {lane_name} lane.", "info")
        return clients[lane_name].models.generate_content(
            model=model_id,
            contents=contents,
            config=config,
        )

    try:
        return _generate_once("standard")
    except Exception as exc:
        last_exc = exc
        if not is_rate_limited_error(exc):
            add_debug_log(
                f"{request_label}: standard lane failed without fallback ({summarize_error(exc)}).",
                "error",
            )
            raise

    if enable_standard_retry:
        wait_before_standard_retry(request_label, last_exc)
        try:
            return _generate_once("standard")
        except Exception as exc:
            last_exc = exc
            if not is_rate_limited_error(exc):
                add_debug_log(
                    (
                        f"{request_label}: standard retry failed without priority fallback "
                        f"({summarize_error(exc)})."
                    ),
                    "error",
                )
                raise
            if allow_priority_fallback:
                add_debug_log(
                    (
                        f"{request_label}: escalating to priority after repeated standard throttling "
                        f"({summarize_error(exc)})."
                    ),
                    "warning",
                )
    elif allow_priority_fallback:
        add_debug_log(
            f"{request_label}: escalating to priority after standard throttling ({summarize_error(last_exc)}).",
            "warning",
        )

    if not allow_priority_fallback:
        raise last_exc

    for retry_number in range(0, 4):
        try:
            return _generate_once("priority")
        except Exception as exc:
            last_exc = exc
            if not is_retryable_priority_error(exc):
                add_debug_log(
                    f"{request_label}: priority lane failed without retry ({summarize_error(exc)}).",
                    "error",
                )
                raise
            if retry_number >= 3:
                add_debug_log(
                    f"{request_label}: priority retries exhausted ({summarize_error(exc)}).",
                    "error",
                )
                raise
            wait_before_priority_retry(request_label, retry_number + 1, exc)

    raise RuntimeError(f"{request_label}: no request attempts were executed.")


def generate_content_stream_with_fallback(
    standard_client,
    priority_client,
    model_id,
    contents,
    config=None,
    request_label="request",
    enable_standard_retry=True,
    allow_priority_fallback=True,
):
    clients = {"standard": standard_client, "priority": priority_client}

    def _runner():
        last_exc = None

        def _stream_lane(lane_name):
            emitted_chunk = False
            add_debug_log(f"{request_label}: using {lane_name} lane.", "info")
            stream = clients[lane_name].models.generate_content_stream(
                model=model_id,
                contents=contents,
                config=config,
            )
            for chunk in stream:
                emitted_chunk = True
                yield chunk
            return emitted_chunk

        standard_emitted_chunk = False
        try:
            for chunk in _stream_lane("standard"):
                standard_emitted_chunk = True
                yield chunk
            return
        except Exception as exc:
            last_exc = exc
            if standard_emitted_chunk or not is_rate_limited_error(exc):
                add_debug_log(
                    f"{request_label}: standard lane failed during stream ({summarize_error(exc)}).",
                    "error",
                )
                raise

        if enable_standard_retry:
            wait_before_standard_retry(request_label, last_exc)
            standard_retry_emitted_chunk = False
            try:
                for chunk in _stream_lane("standard"):
                    standard_retry_emitted_chunk = True
                    yield chunk
                return
            except Exception as exc:
                last_exc = exc
                if standard_retry_emitted_chunk or not is_rate_limited_error(exc):
                    add_debug_log(
                        (
                            f"{request_label}: standard retry failed during stream "
                            f"({summarize_error(exc)})."
                        ),
                        "error",
                    )
                    raise
                if allow_priority_fallback:
                    add_debug_log(
                        (
                            f"{request_label}: escalating to priority after repeated standard throttling "
                            f"({summarize_error(exc)})."
                        ),
                        "warning",
                    )
        elif allow_priority_fallback:
            add_debug_log(
                (
                    f"{request_label}: escalating to priority after standard throttling "
                    f"({summarize_error(last_exc)})."
                ),
                "warning",
            )

        if not allow_priority_fallback:
            raise last_exc

        for retry_number in range(0, 4):
            priority_emitted_chunk = False
            try:
                for chunk in _stream_lane("priority"):
                    priority_emitted_chunk = True
                    yield chunk
                return
            except Exception as exc:
                last_exc = exc
                if priority_emitted_chunk or not is_retryable_priority_error(exc):
                    add_debug_log(
                        f"{request_label}: priority lane failed during stream ({summarize_error(exc)}).",
                        "error",
                    )
                    raise
                if retry_number >= 3:
                    add_debug_log(
                        f"{request_label}: priority retries exhausted during stream ({summarize_error(exc)}).",
                        "error",
                    )
                    raise
                wait_before_priority_retry(request_label, retry_number + 1, exc)

        raise RuntimeError(f"{request_label}: no request attempts were executed.")

    return _runner()


def run_pending_title_generation(standard_client, model_id):
    try:
        title_prompt = (
            "以下の会話を20文字程度の短い日本語タイトルにしてください。"
            "記号は避けてください。\n\n"
            f"{build_title_source_text()}"
        )
        title_cfg = build_title_generation_config(model_id, enable_thinking=True)
        title_res = generate_content_with_fallback(
            standard_client=standard_client,
            priority_client=standard_client,
            model_id=model_id,
            contents=title_prompt,
            config=title_cfg,
            request_label="title_generation",
            enable_standard_retry=False,
            allow_priority_fallback=False,
        )
        date_str = datetime.now().strftime("%y%m%d")
        generated_title = extract_response_text(title_res)
        clean_title = sanitize_generated_title(generated_title)
        used_thinking = supports_thinking(model_id)
        if not clean_title and used_thinking:
            add_debug_log(
                "title_generation: empty response with thinking enabled; retrying without thinking. "
                + summarize_title_response(title_res),
                "warning",
            )
            title_res = generate_content_with_fallback(
                standard_client=standard_client,
                priority_client=standard_client,
                model_id=model_id,
                contents=title_prompt,
                config=build_title_generation_config(model_id, enable_thinking=False),
                request_label="title_generation_retry_no_thinking",
                enable_standard_retry=False,
                allow_priority_fallback=False,
            )
            generated_title = extract_response_text(title_res)
            clean_title = sanitize_generated_title(generated_title)
            used_thinking = False
        add_debug_log(
            "title_generation: "
            f"model={model_id}, thinking={used_thinking}, raw_title={generated_title!r}, "
            f"clean_title={clean_title!r}, {summarize_title_response(title_res)}",
            "info",
        )
        if not clean_title:
            clean_title = "新規チャット"

        new_title = f"{date_str}_{clean_title}"
        if st.session_state.get("chat_title") != new_title:
            st.session_state["chat_title"] = new_title
            bump_chat_revision()
    except Exception as e:
        add_debug_log(f"Title generation failed: {e}", "error")
        if not st.session_state.get("chat_title"):
            st.session_state["chat_title"] = f"{datetime.now().strftime('%y%m%d')}_新規チャット"
            bump_chat_revision()
    finally:
        st.session_state["title_generation_attempted"] = True
        st.session_state["pending_post_action"] = "save"


def run_pending_cloud_save(user_uid):
    can_save, reason = can_autosave(user_uid)
    if not can_save:
        if reason == "user uid unavailable":
            st.session_state["autosave_error"] = "ユーザー認証を待っているため、クラウド保存を保留しています。"
            st.session_state["pending_post_action"] = "save"
        else:
            st.session_state["pending_post_action"] = None
        return

    if not st.session_state.get("chat_id"):
        st.session_state["chat_id"] = uuid.uuid4().hex

    chat_data = session_state_manager.build_snapshot_from_session()
    save_title = st.session_state.get("chat_title")
    if not save_title:
        save_title = f"{datetime.now().strftime('%y%m%d')}_新規チャット"

    try:
        firestore_utils.save_chat_to_firestore(
            uid=user_uid,
            chat_id=st.session_state["chat_id"],
            chat_title=save_title,
            chat_data=chat_data,
            is_encrypted=st.session_state.get("use_encryption", False),
            password=st.session_state.get("encryption_password", ""),
        )
        st.session_state["last_saved_revision"] = st.session_state["chat_revision"]
        st.session_state["autosave_error"] = None
    except Exception as e:
        st.session_state["autosave_error"] = str(e)
        add_debug_log(f"Cloud Save Error: {e}", "error")
    finally:
        st.session_state["pending_post_action"] = None


def run_pending_code_execution():
    try:
        from gp_chat import execution_engine
    except ImportError as e:
        missing_dep_msg = f"**▶️ システム実行エラー:**\n```text\n{e}\n```"
        _append_pending_exec_message_to_history(missing_dep_msg)
        st.session_state["pending_exec_code"] = None
        _clear_exec_retry_state()
        st.session_state["pending_post_action"] = "save"
        _clear_pending_execution_buffers()
        return

    code_to_run = st.session_state.get("pending_exec_code")
    if not code_to_run:
        _clear_exec_retry_state()
        st.session_state["pending_post_action"] = _next_exec_post_action()
        _clear_pending_execution_buffers()
        return

    retry_state = st.session_state.get("exec_retry_state")
    if retry_state:
        file_payloads = copy.deepcopy(retry_state.get("file_payloads") or {})
        canvases = copy.deepcopy(retry_state.get("canvases") or st.session_state.get("python_canvases", []))
    else:
        file_payloads = _collect_current_file_payloads()
        canvases = copy.deepcopy(st.session_state.get("python_canvases", []))

    try:
        execution_result = execution_engine.execute_user_code_detailed(
            code=code_to_run,
            file_payloads=file_payloads,
            canvases=canvases,
        )
        exec_res_md = _build_execution_result_markdown(execution_result)

        retry_state = st.session_state.get("exec_retry_state")
        if retry_state is not None:
            retry_state["last_execution"] = _execution_result_to_retry_state_payload(execution_result)
            st.session_state["exec_retry_state"] = retry_state

        if _should_retry_execution_result(execution_result, retry_state):
            retry_state["attempt_index"] = retry_state.get("attempt_index", 0) + 1
            st.session_state["exec_retry_state"] = retry_state
            st.session_state["pending_exec_code"] = None
            st.session_state["pending_post_action"] = "retry_exec_generation"
        else:
            _append_pending_exec_message_to_history(exec_res_md)
            st.session_state["pending_exec_code"] = None
            _clear_exec_retry_state()
            st.session_state["pending_post_action"] = _next_exec_post_action()
    except Exception as e:
        err_msg = f"**▶️ システム実行エラー:**\n```text\n{e}\n```"
        _append_pending_exec_message_to_history(err_msg)
        st.session_state["pending_exec_code"] = None
        _clear_exec_retry_state()
        st.session_state["pending_post_action"] = _next_exec_post_action()
    finally:
        _clear_pending_execution_buffers()


def _run_pending_exec_retry_generation(standard_client, priority_client, model_id, max_tokens_val):
    retry_state = st.session_state.get("exec_retry_state")
    if not retry_state:
        _clear_exec_retry_state()
        st.session_state["pending_post_action"] = _next_exec_post_action()
        return

    system_message = ""
    if st.session_state.get("messages") and st.session_state["messages"][0]["role"] == "system":
        system_message = st.session_state["messages"][0]["content"]

    retry_prompt = _build_exec_retry_prompt(retry_state)
    cfg = types.GenerateContentConfig(
        system_instruction=system_message,
        max_output_tokens=max_tokens_val,
        tools=[],
    )

    if supports_thinking(model_id):
        effort = st.session_state.get("reasoning_effort", "high")
        thinking_level = types.ThinkingLevel.HIGH if effort == "high" else types.ThinkingLevel.LOW
        cfg.thinking_config = types.ThinkingConfig(
            thinking_level=thinking_level,
            include_thoughts=False,
        )

    contents = [types.Content(role="user", parts=[types.Part.from_text(text=retry_prompt)])]

    try:
        retry_response = generate_content_with_fallback(
            standard_client=standard_client,
            priority_client=priority_client,
            model_id=model_id,
            contents=contents,
            config=cfg,
            request_label="exec_retry_generation",
            enable_standard_retry=True,
            allow_priority_fallback=True,
        )
        retry_text = extract_response_text(retry_response)
        retry_usage = _usage_dict_from_usage_metadata(getattr(retry_response, "usage_metadata", None))
        retry_state["usage_totals"] = _merge_usage_totals(retry_state.get("usage_totals"), retry_usage)

        python_code = _extract_latest_python_block(retry_text)
        if not python_code:
            err_md = (
                "**▶️ コード再生成エラー:**\n```text\n"
                "自動リトライで Python コードを再生成できませんでした。\n```"
            )
            _append_pending_exec_message_to_history(err_md)
            st.session_state["pending_exec_code"] = None
            _clear_exec_retry_state()
            st.session_state["pending_post_action"] = _next_exec_post_action()
            return

        candidate_message = {"role": "assistant", "content": retry_text}
        st.session_state["pending_exec_message"] = copy.deepcopy(candidate_message)
        retry_state["candidate_message"] = copy.deepcopy(candidate_message)
        retry_state["last_code"] = python_code
        st.session_state["exec_retry_state"] = retry_state
        st.session_state["pending_exec_code"] = python_code
        st.session_state["pending_post_action"] = "run_code"
    except Exception as e:
        err_md = f"**▶️ コード再生成エラー:**\n```text\n{e}\n```"
        _append_pending_exec_message_to_history(err_md)
        st.session_state["pending_exec_code"] = None
        _clear_exec_retry_state()
        st.session_state["pending_post_action"] = _next_exec_post_action()


def load_history(uploader_key):
    uploaded_file = st.session_state.get(uploader_key)
    if not uploaded_file:
        return

    try:
        loaded_data = json.load(uploaded_file)
        session_state_manager.queue_restore_snapshot(loaded_data, source="local")
        st.session_state["pending_restore_notice"] = config.UITexts.HISTORY_LOADED_SUCCESS
    except Exception as e:
        st.error(f"読込に失敗しました: {e}")


def embed_timeout_warning_js():
    """
    ユーザーの最後の操作から3500秒(58分20秒)経過後に、ブラウザのアラートを表示するJSを埋め込む。
    画面が再描画されるたびにこのJSも再実行され、タイマーがリセットされる。
    """
    js_code = """
    <script>
    // 親ウィンドウのグローバル変数にタイマーを持たせ、多重起動を防ぐ
    if (window.parent.appTimeoutTimer) {
        clearTimeout(window.parent.appTimeoutTimer);
    }

    window.parent.appTimeoutTimer = setTimeout(function() {
        try {
            window.parent.alert("⚠️【システム警告】\\n\\nアプリ上で約1時間操作がないため、まもなくセッションが終了し画面がリセットされます。\\n\\n作業を再開・継続する場合は、このアラートを閉じて画面をリロードするか、チャットを送信してください。");
        } catch (e) {
            // iframeの制約などで親へのアクセスが失敗した場合のフォールバック
            alert("⚠️【システム警告】\\nまもなくセッションが終了します。");
        }
    }, 3500000); // 3500秒 = 3,500,000 ms
    </script>
    """
    components.html(js_code, height=0, width=0)


def run_chatbot_app():
    st.set_page_config(page_title=config.UITexts.APP_TITLE, layout="wide")
    st.title(config.UITexts.APP_TITLE)

    user_uid = "unknown"
    user_email = "unknown"
    try:
        if hasattr(st, "context"):
            headers = st.context.headers
            if "X-User-UID" in headers and headers["X-User-UID"] != "unknown":
                user_uid = headers["X-User-UID"]
            if "X-User-Email" in headers and headers["X-User-Email"] != "unknown":
                user_email = headers["X-User-Email"]

            cookies = st.context.cookies
            if (user_uid == "unknown" or user_email == "unknown") and "session" in cookies:
                from firebase_admin import auth

                session_cookie = cookies["session"]
                decoded_claims = auth.verify_session_cookie(session_cookie, check_revoked=False)
                if user_uid == "unknown":
                    user_uid = decoded_claims.get("uid", "unknown")
                if user_email == "unknown":
                    user_email = decoded_claims.get("email", "unknown")
    except Exception as e:
        add_debug_log(f"UID fetch error: {e}", "error")

    prompts = utils.load_prompts()
    app_config = utils.load_app_config()
    supported_extensions = app_config.get("file_uploader", {}).get("supported_extensions", [])
    env_files = utils.find_env_files()
    if not env_files and os.path.isfile(".env"):
        env_files = [".env"]
    is_cloud_env = os.getenv("GCP_PROJECT_ID") is not None

    if not env_files and not is_cloud_env:
        st.error("設定エラー: .env ファイルが見つからず、環境変数も設定されていません。")
        st.stop()

    for key, value in config.SESSION_STATE_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)

    try:
        session_state_manager.apply_queued_restore_if_any()
    except Exception as e:
        st.session_state["pending_restore_payload"] = None
        st.session_state["pending_restore_source"] = None
        st.session_state.pop("pending_restore_notice", None)
        st.error(f"復元に失敗しました: {e}")

    if st.session_state.get("pending_restore_notice"):
        st.toast(st.session_state.pop("pending_restore_notice"), icon="✅")

    if env_files:
        selected_env = st.session_state.get("selected_env_file")
        if selected_env not in env_files:
            selected_env = env_files[0]
            st.session_state["selected_env_file"] = selected_env
        load_dotenv(dotenv_path=selected_env, override=True)

    configured_model_id = os.getenv(config.GEMINI_MODEL_ID_NAME, config.DEFAULT_MODEL_ID)
    if configured_model_id and configured_model_id not in config.AVAILABLE_MODELS:
        config.AVAILABLE_MODELS = [configured_model_id, *config.AVAILABLE_MODELS]
    if not st.session_state.get("model_selection_initialized", False):
        current_model_id = st.session_state.get("current_model_id")
        default_model_id = config.SESSION_STATE_DEFAULTS.get("current_model_id")
        if current_model_id in (None, "", default_model_id):
            st.session_state["current_model_id"] = configured_model_id
        st.session_state["model_selection_initialized"] = True

    project_id = os.getenv(config.GCP_PROJECT_ID_NAME)
    location = os.getenv(config.GCP_LOCATION_NAME, "us-central1")
    model_id = st.session_state.get("current_model_id") or configured_model_id
    title_model_id = os.getenv("TITLE_GENERATION_MODEL_ID", "gemini-3.5-flash")
    max_tokens_val = min(int(os.getenv("MAX_TOKEN", "65536")), 65536)

    try:
        standard_client = build_vertex_client(project_id, location, priority=False)
        priority_client = build_vertex_client(project_id, location, priority=True)
    except Exception as e:
        st.error(f"クライアント初期化エラー: {e}")
        st.stop()

    def mark_canvas_changed():
        bump_chat_revision()
        st.session_state["pending_post_action"] = "save"

    def handle_clear_canvas(index):
        st.session_state["python_canvases"][index] = config.ACE_EDITOR_DEFAULT_CODE
        st.session_state["canvas_key_counter"] += 1
        mark_canvas_changed()

    def handle_canvas_file_upload(index, uploader_key):
        uploaded = st.session_state.get(uploader_key)
        if not uploaded:
            return
        st.session_state["python_canvases"][index] = uploaded.getvalue().decode("utf-8")
        mark_canvas_changed()

    sidebar.render_sidebar(
        supported_extensions,
        env_files,
        load_history,
        handle_clear_canvas,
        handle_canvas_file_upload,
        mark_canvas_changed,
        user_uid=user_uid,
    )

    if (
        st.session_state.get("pending_post_action") is None
        and not st.session_state.get("is_generating", False)
        and is_default_chat_title(st.session_state.get("chat_title"))
        and not st.session_state.get("placeholder_title_retry_attempted", False)
        and should_generate_title()
    ):
        st.session_state["placeholder_title_retry_attempted"] = True
        st.session_state["pending_post_action"] = "generate_title"
        add_debug_log("title_generation: retrying placeholder title once.", "info")
        st.rerun()
        return

    if st.session_state.get("pending_post_action") == "generate_title" and not st.session_state.get("is_generating", False):
        run_pending_title_generation(standard_client, title_model_id)
        st.rerun()
        return

    if st.session_state.get("pending_post_action") == "retry_exec_generation" and not st.session_state.get("is_generating", False):
        _run_pending_exec_retry_generation(standard_client, priority_client, model_id, max_tokens_val)
        st.rerun()
        return

    if st.session_state.get("pending_post_action") == "save" and not st.session_state.get("is_generating", False):
        prev_saved_revision = st.session_state.get("last_saved_revision", 0)
        run_pending_cloud_save(user_uid)
        if st.session_state.get("last_saved_revision", 0) != prev_saved_revision:
            st.rerun()
            return

    pending_generation_error = st.session_state.pop("pending_generation_error", None)
    if pending_generation_error:
        st.error(pending_generation_error)

    with st.expander("🛠 システムログ", expanded=False):
        for log in reversed(st.session_state["debug_logs"]):
            st.text(log)

    if st.session_state.get("autosave_error"):
        st.warning(f"クラウド保存エラー: {st.session_state['autosave_error']}")

    if not st.session_state["system_role_defined"]:
        st.subheader("AIの役割を設定")
        role = st.text_area(
            "システムロール",
            value=prompts.get(
                "system",
                {},
            ).get(
                "text",
                "あなたは優秀なデータサイエンティストです。データ分析やグラフ作成が必要な場合は、Pythonコードを生成してください。システムが自動で実行します。",
            ),
            height=200,
        )
        if st.button("チャットを開始", type="primary"):
            st.session_state["messages"] = [{"role": "system", "content": role}]
            st.session_state["system_role_defined"] = True
            st.rerun()

        # 起動直後の画面にもタイマーだけは仕込んでおく
        embed_timeout_warning_js()
        st.stop()

    for msg in st.session_state["messages"]:
        if msg["role"] != "system":
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"], unsafe_allow_html=True)
                if "grounding_metadata" in msg and msg["grounding_metadata"]:
                    with st.expander("🔎 検索ソース (Grounding)"):
                        st.json(msg["grounding_metadata"])
                if msg["role"] == "assistant" and "usage" in msg:
                    u = msg["usage"]
                    if u.get("input_tokens") is None or u.get("output_tokens") is None:
                        continue
                    st.caption(f"トークン数: 入力 {u['input_tokens']:,} / 出力 {u['output_tokens']:,}")

    if (
        st.session_state.get("pending_post_action") == "run_code"
        and not st.session_state.get("is_generating", False)
    ):
        with st.status("Executing generated Python code...", expanded=True) as exec_status:
            exec_status.write("Running the latest ```python``` block in the sandbox.")
            run_pending_code_execution()
            exec_status.update(label="Code execution finished.", state="complete", expanded=False)
        st.rerun()
        return

    if prompt := st.chat_input("指示を入力...", disabled=st.session_state.get("is_generating", False)):
        _clear_exec_retry_state()
        st.session_state["pending_exec_code"] = None
        st.session_state["messages"].append({"role": "user", "content": prompt})
        st.session_state["pending_ai_task_id"] = str(uuid.uuid4())
        bump_chat_revision()
        st.session_state["is_generating"] = True
        st.rerun()

    if st.session_state["is_generating"]:
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
            ai_task_id = st.session_state.get("pending_ai_task_id") or str(uuid.uuid4())
            st.session_state["pending_ai_task_id"] = ai_task_id

            is_special = bool(st.session_state.get("special_generation_messages"))
            msgs = st.session_state["special_generation_messages"] if is_special else st.session_state["messages"]

            contents = []
            sys_inst = ""
            for m in msgs:
                if m["role"] == "system":
                    sys_inst = m["content"]
                else:
                    contents.append(
                        types.Content(
                            role=m["role"],
                            parts=[types.Part.from_text(text=_sanitize_message_content_for_model(m["content"]))],
                        )
                    )

            if st.session_state.get("auto_plot_enabled", False) and not is_special:
                sys_inst += (
                    "\n\n【システム設定】現在「データ分析モード(Python自動実行)」がONです。"
                    "ユーザーの要求に応じて、データ分析やグラフ作成が必要な場合はPythonコードを生成してください。"
                    "コードは必ず ```python と ``` で囲んでください。"
                    "アップロードされたファイルへのパスは辞書 `files` から `files['ファイル名']` で取得可能です。"
                )

            if st.session_state.get("auto_plot_enabled", False) and not is_special:
                sys_inst += (
                    "\n\nCode execution sandbox rules:\n"
                    "- If code execution is useful, return runnable Python code inside a ```python fenced block.\n"
                    "- Network access is forbidden during code execution.\n"
                    "- Only uploaded files exposed via `files[...]` and files created under the current working directory may be accessed.\n"
                    "- Do not read absolute paths, parent directories, `/app`, or environment files.\n"
                    "- If you need output files, write them using relative paths inside the current working directory.\n"
                    "- The execution environment already configures Japanese-capable Matplotlib fonts when available.\n"
                    "- Do not import `japanize_matplotlib`.\n"
                    "- Do not overwrite `plt.rcParams['font.family']`, `matplotlib.rcParams['font.family']`, or related font settings unless explicitly requested.\n"
                    "- If Japanese labels are needed, use Japanese strings directly without adding custom font configuration.\n"
                )

            combined_queue = st.session_state.get("uploaded_file_queue", []) + st.session_state.get("clipboard_queue", [])
            if not is_special:
                if combined_queue:
                    parts, _ = utils.process_uploaded_files_for_gemini(combined_queue)
                    if parts and contents:
                        contents[-1].parts = parts + contents[-1].parts

                canvas_parts = []
                for i, canvas_code in enumerate(st.session_state["python_canvases"]):
                    if canvas_code.strip() and canvas_code != config.ACE_EDITOR_DEFAULT_CODE:
                        canvas_parts.append(
                            types.Part.from_text(text=f"\n[Canvas-{i + 1}]\n```python\n{canvas_code}\n```")
                        )
                if canvas_parts and contents:
                    contents[-1].parts = canvas_parts + contents[-1].parts

            effort = st.session_state.get("reasoning_effort", "high")
            thinking_level = types.ThinkingLevel.HIGH if effort == "high" else types.ThinkingLevel.LOW
            tools = (
                [types.Tool(google_search=types.GoogleSearch())]
                if st.session_state.get("enable_google_search") and not is_special
                else []
            )

            try:
                request_label = "special_generation" if is_special else "chat_generation"
                cfg = types.GenerateContentConfig(
                    system_instruction=sys_inst,
                    max_output_tokens=max_tokens_val,
                    tools=tools,
                )
                if supports_thinking(model_id):
                    cfg.thinking_config = types.ThinkingConfig(
                        thinking_level=thinking_level,
                        include_thoughts=True,
                    )

                stream = generate_content_stream_with_fallback(
                    standard_client=standard_client,
                    priority_client=priority_client,
                    model_id=model_id,
                    contents=contents,
                    config=cfg,
                    request_label=request_label,
                    enable_standard_retry=True,
                    allow_priority_fallback=True,
                )

                for chunk in stream:
                    if chunk.usage_metadata:
                        usage_meta = chunk.usage_metadata
                    if not chunk.candidates:
                        continue
                    cand = chunk.candidates[0]

                    if cand.grounding_metadata:
                        grounding_chunks.append(cand.grounding_metadata)
                        if cand.grounding_metadata.web_search_queries:
                            for q in cand.grounding_metadata.web_search_queries:
                                full_thought += f"\n\n🔍 検索: `{q}`\n\n"
                                thought_ph.markdown(full_thought)

                    if cand.content and cand.content.parts:
                        for part in cand.content.parts:
                            is_thought = False
                            txt = ""
                            if hasattr(part, "thought") and part.thought:
                                is_thought = True
                                txt = part.thought if isinstance(part.thought, str) else part.text

                            if is_thought:
                                full_thought += txt
                                thought_ph.markdown(full_thought)
                            elif part.text:
                                full_res += part.text
                                text_ph.markdown(full_res + "▌")

                text_ph.markdown(full_res)
                if not full_thought:
                    thought_area.empty()
                else:
                    thought_status.update(label="思考完了", state="complete")

                final_grounding = {}
                if grounding_chunks:
                    last = grounding_chunks[-1]
                    if last.grounding_chunks:
                        srcs = [{"title": g.web.title, "uri": g.web.uri} for g in last.grounding_chunks if g.web]
                        if srcs:
                            final_grounding["sources"] = srcs
                    if last.web_search_queries:
                        final_grounding["queries"] = last.web_search_queries

                usage_dict = _usage_dict_from_usage_metadata(usage_meta)
                if usage_dict:
                    if not st.session_state.get("chat_id"):
                        st.session_state["chat_id"] = uuid.uuid4().hex
                    try:
                        _emit_ai_usage_log(
                            user_email=user_email,
                            task_id=ai_task_id,
                            model_id=model_id,
                            usage=usage_dict,
                            additional_info={
                                "chat_id": st.session_state.get("chat_id"),
                                "request_label": request_label,
                            },
                        )
                    except Exception as log_exc:
                        add_debug_log(f"Cloud Logging Error: {log_exc}", "error")

                assistant_msg = {"role": "assistant", "content": full_res}
                if usage_dict:
                    assistant_msg["usage"] = usage_dict
                if final_grounding:
                    assistant_msg["grounding_metadata"] = final_grounding

                if is_special:
                    for m in msgs:
                        if m["role"] == "user":
                            st.session_state["messages"].append(m)
                    del st.session_state["special_generation_messages"]

                if st.session_state.get("auto_plot_enabled", False) and not is_special:
                    latest_code = _extract_latest_python_block(full_res)
                    if latest_code:
                        file_payloads = _collect_current_file_payloads()
                        canvases = copy.deepcopy(st.session_state.get("python_canvases", []))
                        st.session_state["pending_exec_message"] = copy.deepcopy(assistant_msg)
                        st.session_state["exec_retry_state"] = _build_initial_exec_retry_state(
                            candidate_message=assistant_msg,
                            last_code=latest_code,
                            file_payloads=file_payloads,
                            canvases=canvases,
                            usage_totals=usage_dict,
                            initial_grounding_metadata=final_grounding if final_grounding else None,
                            max_attempts=2,
                        )
                        st.session_state["pending_exec_code"] = latest_code
                        st.session_state["pending_post_action"] = "run_code"
                    else:
                        _clear_exec_retry_state()
                        st.session_state["messages"].append(assistant_msg)
                        bump_chat_revision()
                        st.session_state["pending_post_action"] = _next_exec_post_action()
                else:
                    _clear_exec_retry_state()
                    st.session_state["messages"].append(assistant_msg)
                    bump_chat_revision()
                    st.session_state["pending_post_action"] = _next_exec_post_action()
            except Exception as e:
                add_debug_log(
                    f"{request_label}: final failure after fallback handling ({summarize_error(e)}).",
                    "error",
                )
                if full_thought:
                    thought_status.update(label="Generation failed", state="error", expanded=True)
                else:
                    thought_area.empty()
                if full_res:
                    text_ph.markdown(full_res)
                st.session_state["pending_generation_error"] = f"生成エラー: {e}"
                if is_special:
                    st.session_state.pop("special_generation_messages", None)
                can_save, _ = can_autosave(user_uid)
                if can_save:
                    st.session_state["pending_post_action"] = "save"
            finally:
                st.session_state["is_generating"] = False
                st.session_state.pop("pending_ai_task_id", None)

                if not is_special and st.session_state.get("pending_post_action") != "run_code":
                    # 通常アップロードは残し、クリップボード貼付けだけ消す
                    st.session_state["clipboard_queue"] = []

        st.rerun()
        return

    # ★ 画面の描画完了時にJSタイマーを埋め込む
    embed_timeout_warning_js()


if __name__ == "__main__":
    run_chatbot_app()