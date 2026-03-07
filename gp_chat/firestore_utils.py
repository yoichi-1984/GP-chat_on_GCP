import json
import base64
import hashlib
from datetime import datetime
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import firebase_admin
from firebase_admin import firestore

# Firebase Adminの初期化（安全な呼び出し）
try:
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client(database_id="gp-chat-db")
except Exception as e:
    print(f"[Firestore Init Error] {e}", flush=True)
    db = None

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

def save_chat_to_firestore(uid: str, chat_title: str, messages: list, is_encrypted: bool, password: str):
    """チャット履歴をFirestoreに保存する"""
    if not uid or uid == "unknown" or db is None: 
        return

    try:
        doc_ref = db.collection("users").document(uid).collection("chat_histories").document(chat_title)
        chat_data_str = json.dumps({"messages": messages}, ensure_ascii=False)
        
        save_title = encrypt_text(chat_title, password) if is_encrypted else chat_title
        save_data = encrypt_text(chat_data_str, password) if is_encrypted else chat_data_str

        doc_ref.set({
            "display_title": save_title,
            "chat_data": save_data,
            "is_encrypted": is_encrypted,
            "updated_at": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print(f"[Firestore Save Error] {e}", flush=True)

def get_history_list(uid: str, password: str) -> list:
    """Firestoreから履歴一覧を取得する"""
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