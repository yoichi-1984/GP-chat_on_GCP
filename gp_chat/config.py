# config.py:
MAX_CANVASES = 20
GCP_PROJECT_ID_NAME = "GCP_PROJECT_ID"
GCP_LOCATION_NAME = "GCP_LOCATION"
GEMINI_MODEL_ID_NAME = "GEMINI_MODEL_ID"

ACE_EDITOR_SETTINGS = {
    "language": "python", 
    "theme": "monokai", 
    "font_size": 14, 
    "show_gutter": True, 
    "wrap": False
}
ACE_EDITOR_DEFAULT_CODE = "# Code goes here\n"
DEFAULT_SYSTEM_ROLE = "You are Gemini, a helpful AI assistant."

# ==============================================================================
# セッション状態のデフォルト値 (リセット時に完全に初期化される基準)
# ==============================================================================
SESSION_STATE_DEFAULTS = {
    # --- チャット・プロンプト関連 ---
    "messages": [], 
    "system_role_defined": False, 
    "chat_title": None,
    
    # --- UI・機能設定 (スナップショット対象) ---
    "current_model_id": "gemini-3.1-pro-preview",
    "reasoning_effort": "high", 
    "enable_google_search": False, 
    "auto_plot_enabled": False,  # ★追加: データ分析モードのフラグ
    "multi_code_enabled": False, 
    
    # --- エディタ関連 ---
    "python_canvases": ["# Code goes here\n"],
    "canvas_key_counter": 0,
    
    # --- システム状態・制御フラグ ---
    "total_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    "is_generating": False, 
    "last_usage_info": None, 
    "stop_generation": False, 
    "debug_logs": [], 
    
    # --- ファイルアップロード関連 (リセット対象だがスナップショットには含めない) ---
    "uploaded_file_queue": [],
    "clipboard_queue": [],       # ★追加: クリップボードのキュー
    "uploader_key_counter": 0,   # ★追加: UI強制リセット用カウンター
    "clear_uploader": False      # ★追加: UIリセットトリガー
}

# ==============================================================================
# スナップショット対象キー (クラウド保存・JSONダウンロード時に抽出・復元する項目)
# ==============================================================================
# ※ 添付ファイル等の「その場限りの状態」は除外し、チャットのコンテキストと設定のみを定義
SNAPSHOT_KEYS = [
    "messages",
    "system_role_defined",
    "chat_title",
    "current_model_id",
    "reasoning_effort",
    "enable_google_search",
    "auto_plot_enabled",
    "multi_code_enabled",
    "python_canvases"
]

AVAILABLE_MODELS = ["gemini-3.1-pro-preview","gemini-3-pro-preview", "gemini-3-flash-preview"]

class UITexts:
    APP_TITLE = "🤖GP-Chat 汎用AIアプリ with Gemini"
    SIDEBAR_HEADER = "設定"
    RESET_BUTTON_LABEL = "会話履歴をリセット"
    CODEX_MINI_INFO = "`Gemini 3 は最大1Mまでのトークンを使用可能です` ."
    HISTORY_SUBHEADER = "会話履歴 (JSON)"
    DOWNLOAD_HISTORY_BUTTON = "会話履歴をダウンロード"
    UPLOAD_HISTORY_LABEL = "JSONで会話を再開"
    HISTORY_LOADED_SUCCESS = "会話履歴と設定を完全に復元しました" # ★文言を実態に合わせて修正
    OLD_HISTORY_FORMAT_WARNING = "古いフォーマットなので対応していません"
    JSON_FORMAT_ERROR = "対応できないJSON形式です"
    JSON_LOAD_ERROR = "JSON load error: {e}"
    EDITOR_SUBHEADER = "🔧 コードエディタ"
    MULTI_CODE_CHECKBOX = "マルチコードを有効化"
    ADD_CANVAS_BUTTON = "Canvasを追加"
    CLEAR_BUTTON = "クリア"
    REVIEW_BUTTON = "レビュー"
    VALIDATE_BUTTON = "検証"
    FILE_UPLOAD_HEADER = "📂 ファイル添付"
    FILE_UPLOAD_LABEL = "画像 / PDF / Word / PPT"
    FILE_UPLOAD_HELP = "チャット送信時にAIに読み込ませます。送信後にクリアされます。"
    SUPPORTED_FILE_TYPES = ["png", "jpg", "jpeg", "bmp", "gif", "pdf", "docx", "pptx", "ppt", "txt", "md"]
    SYSTEM_PROMPT_HEADER = "Set AI System Role"
    SYSTEM_PROMPT_TEXT_AREA_LABEL = "System Role"
    START_CHAT_BUTTON = "Start Chat"
    ENV_VARS_ERROR = "Error: Environment variable '{vars}' is not set."
    CLIENT_INIT_ERROR = "SDK initialization failed: {e}"
    API_REQUEST_ERROR = "API request failed: {e}"
    NO_CODE_TO_VALIDATE = "No code to validate."
    VALIDATE_SPINNER_MULTI = "Validating Canvas-{i}..."
    VALIDATE_SPINNER_SINGLE = "Validating code..."
    PYLINT_SYNTAX_ERROR = "⚠️ Syntax error detected by pylint."
    STOP_GENERATION_BUTTON = "Stop"
    CHAT_INPUT_PLACEHOLDER = "Message Gemini..."
    REVIEW_PROMPT_SINGLE = "### Reference Code (Canvas)\nPlease review this code and suggest improvements."
    REVIEW_PROMPT_MULTI = "### Reference Code (Canvas-{i})\nPlease review this canvas and suggest improvements."
    WEB_SEARCH_LABEL = "Web検索 (Grounding)"
    WEB_SEARCH_HELP = "Google検索を使用して回答を生成します。"