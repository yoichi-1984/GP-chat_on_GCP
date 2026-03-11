# firestore_utils.py:
import os
import json
import base64
import hashlib
import yaml
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import firebase_admin
from firebase_admin import firestore
from firebase_admin import storage

# Firebase Adminの初期化（安全な呼び出し）
try:
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client(database_id="gp-chat-db")
except Exception as e:
    print(f"[Firebase Init Error] {e}", flush=True)
    db = None

def get_bucket():
    """Cloud Storageのバケットオブジェクトを取得する"""
    # 1. まずは環境変数を確認 (Cloud Runのコンソール設定などを優先)
    bucket_name = os.getenv("FIREBASE_STORAGE_BUCKET")
    
    # 2. 環境変数がなければ、bucket.yaml から読み込みを試みる
    if not bucket_name:
        try:
            # main.pyの実行ディレクトリ(ルート)にある想定
            yaml_path = os.path.join(os.getcwd(), "bucket.yaml")
            if os.path.exists(yaml_path):
                with open(yaml_path, "r", encoding="utf-8") as f:
                    bucket_config = yaml.safe_load(f)
                    bucket_name = bucket_config.get("FIREBASE_STORAGE_BUCKET")
        except Exception as e:
            print(f"[Config Warning] Failed to load bucket.yaml: {e}")

    # 3. それでもなければプロジェクトIDから推論 (最終フォールバック)
    if not bucket_name:
        project_id = os.getenv("GCP_PROJECT_ID")
        if project_id:
            bucket_name = f"{project_id}.appspot.com"
        else:
            raise ValueError("Environment variable FIREBASE_STORAGE_BUCKET, bucket.yaml, or GCP_PROJECT_ID is not set.")
            
    return storage.bucket(bucket_name)

def verify_crypto_password(uid: str, password: str) -> bool:
    """ユーザーの暗号化パスワードを検証し、初回なら固定する"""
    if not password or uid == "unknown" or db is None: 
        return False
    
    try:
        doc_ref = db.collection("users").document(uid).collection("settings").document("crypto")
        doc = doc_ref.get()
        
        # セキュリティのためハッシュ化して保存・比較
        hashed_pw = hashlib.sha256(password.encode()).hexdigest()
        
        if doc.exists:
            stored_hash = doc.to_dict().get("password_hash")
            return stored_hash == hashed_pw
        else:
            # 初回登録の場合はパスワードを固定化
            doc_ref.set({"password_hash": hashed_pw})
            return True
    except Exception as e:
        print(f"[Firestore Verify Error] {e}", flush=True)
        return False

def get_crypto_key(password: str) -> bytes:
    """パスワードから32バイトの暗号化キーを生成する"""
    salt = b'gp_chat_static_salt_for_security'
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))

def encrypt_text(text: str, password: str) -> str:
    """平文を暗号化する"""
    if not password: return text
    try:
        f = Fernet(get_crypto_key(password))
        return f.encrypt(text.encode('utf-8')).decode('utf-8')
    except Exception:
        return text

def decrypt_text(encrypted_text: str, password: str) -> str:
    """暗号文を復号する（失敗時はそのまま返す）"""
    if not password: return encrypted_text
    try:
        f = Fernet(get_crypto_key(password))
        return f.decrypt(encrypted_text.encode('utf-8')).decode('utf-8')
    except Exception:
        return encrypted_text # 復号失敗時は文字化けしたまま返す

def save_chat_to_firestore(uid: str, chat_title: str, chat_data: dict, is_encrypted: bool, password: str):
    """
    【改修】チャット履歴全体(JSON)をCloud Storageに保存し、
    Firestoreには1MB制限を回避するためメタデータ(URI等)のみを保存する。
    """
    if not uid or uid == "unknown" or db is None: 
        raise ValueError("ユーザー認証情報がないか、データベースが接続されていません。")

    try:
        # 1. 保存するJSONデータ(実体)の準備と暗号化
        chat_data_str = json.dumps(chat_data, ensure_ascii=False)
        save_data = encrypt_text(chat_data_str, password) if is_encrypted else chat_data_str

        # 2. Cloud Storageへのアップロード
        bucket = get_bucket()
        # Storageのパス (ユーザーごとの隔離フォルダ)
        blob_path = f"users/{uid}/chats/{chat_title}.json"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(save_data, content_type="application/json")

        # 3. Firestoreへのメタデータ保存 (目次のみ)
        save_title = encrypt_text(chat_title, password) if is_encrypted else chat_title
        doc_ref = db.collection("users").document(uid).collection("chat_histories").document(chat_title)
        
        doc_ref.set({
            "display_title": save_title,
            "storage_path": blob_path,  # Storageの保存先パスを記録
            "is_encrypted": is_encrypted,
            "updated_at": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print(f"[Cloud Save Error] {e}", flush=True)
        raise e  # エラーを握り潰さず、main.py に伝えてUIに表示させる

def get_history_list(uid: str, password: str) -> list:
    """
    Firestoreから履歴のメタデータ一覧を取得する。
    ※ここでは実データ(JSON)はまだダウンロードせず、一覧表示用データのみを返す。
    """
    if not uid or uid == "unknown" or db is None: 
        return []
        
    try:
        docs = db.collection("users").document(uid).collection("chat_histories").order_by("updated_at", direction=firestore.Query.DESCENDING).get()
        
        histories = []
        for doc in docs:
            data = doc.to_dict()
            display_title = data.get("display_title", doc.id)
            
            # 暗号化されており、パスワードが渡されている場合のみ復号を試みる
            if data.get("is_encrypted") and password:
                decrypted_title = decrypt_text(display_title, password)
                histories.append({"id": doc.id, "title": decrypted_title, "raw_data": data})
            else:
                # パスワードがない・間違っている場合は文字化けしたまま一覧に出す
                histories.append({"id": doc.id, "title": display_title, "raw_data": data})
                
        return histories
    except Exception as e:
        print(f"[Firestore Fetch Error] {e}", flush=True)
        return []

def load_chat_from_cloud(storage_path: str, is_encrypted: bool, password: str) -> dict:
    """
    【新規追加】Cloud Storageから実データ(JSON)をダウンロードし、
    必要に応じて復号してから辞書(dict)として返す。
    """
    if not storage_path:
        raise ValueError("Storage path is missing.")
        
    try:
        bucket = get_bucket()
        blob = bucket.blob(storage_path)
        
        if not blob.exists():
            raise FileNotFoundError(f"File not found in storage: {storage_path}")
            
        downloaded_bytes = blob.download_as_string()
        data_str = downloaded_bytes.decode('utf-8')
        
        # 復号処理
        if is_encrypted:
            data_str = decrypt_text(data_str, password)
            
        return json.loads(data_str)
    except Exception as e:
        print(f"[Cloud Load Error] {e}", flush=True)
        raise e