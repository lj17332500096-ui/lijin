APPLICANT_USERS = {"applicant": "app123"}

"""Prüfer: Prüfer, der Anträge prüft und bewertest."""
REVIEWER_USERS = {"reviewer": "rev456"}

"""Administrator: Systemadministrator mit voller Kontrolle."""
ADMIN_USERS = {"admin": "admin789"}

import threading
import time
import hashlib
import uuid

USERS = {**APPLICANT_USERS, **REVIEWER_USERS, **ADMIN_USERS}

# 持久化所有已创建的 session，key 为 token 字符串
SESSIONS: dict[str, dict] = {}
SESSION_LOCK = threading.Lock()
_SESSION_COUNTER = 0


def _generate_token(username):
    """生成唯一 token，使用计数器保证即使在 mock 时间下也不同。"""
    global _SESSION_COUNTER
    _SESSION_COUNTER += 1
    raw = f"{username}-{time.time()}-{uuid.uuid4().hex[:8]}-{_SESSION_COUNTER}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"session-{token_hash}-{username}"


def login(username, password):
    """验证用户名密码，成功后自动创建 session 并返回 True，失败返回 False。"""
    if username not in USERS or USERS[username] != password:
        return False
    create_session(username)
    return True


def is_logged_in(session):
    """通过 token 查持久化 session，判断是否仍在有效期。"""
    if not session or "token" not in session:
        return False
    token = session["token"]
    with SESSION_LOCK:
        persisted = SESSIONS.get(token)
    if not persisted:
        return False
    return persisted["expire"] > time.time()


def create_session(username, language="zh-CN"):
    import hashlib
    role = "user"
    for role_name, role_users in [("applicant", APPLICANT_USERS), ("reviewer", REVIEWER_USERS), ("admin", ADMIN_USERS)]:
        if username in role_users:
            role = role_name
            break
    # 用 SHA-256 对用户名哈希生成唯一 token，避免不同用户共用同一 token
    token = _generate_token(username)
    session_data = {
        "token": token,
        "expire": time.time() + 3600,
        "role": role,
        "username": username,
        "language": language,
    }
    # 持久化到全局字典
    with SESSION_LOCK:
        SESSIONS[token] = session_data
    return session_data


def get_role(session):
    """Devuelve el rol del usuario logueado."""
    if is_logged_in(session):
        token = session.get("token")
        with SESSION_LOCK:
            persisted = SESSIONS.get(token)
        if persisted:
            return persisted.get("role", "user")
    return None


def get_language(session):
    """获取用户的语言设置，默认为 zh-CN。"""
    if is_logged_in(session):
        token = session.get("token")
        with SESSION_LOCK:
            persisted = SESSIONS.get(token)
        if persisted:
            return persisted.get("language", "zh-CN")
    return None


def refresh_token(session):
    import time
    import hashlib
    # Preserva el rol y nombre de usuario, solo refresca token y expire
    token = session.get("token")
    with SESSION_LOCK:
        persisted = SESSIONS.get(token)
    if persisted:
        username = persisted.get("username", "unknown")
        language = persisted.get("language", "zh-CN")
        role = persisted.get("role", "user")
    else:
        # Token 不在 SESSIONS 中，从 session 参数获取信息
        username = session.get("username", "unknown")
        language = session.get("language", "zh-CN")
        role = session.get("role", "user")
    # 使用带计数器的 token 生成器，确保即使时间被 mock 为固定值也能生成唯一 token
    new_token = _generate_token(username)
    new_expire = time.time() + 3600
    with SESSION_LOCK:
        SESSIONS[new_token] = {
            "token": new_token,
            "expire": new_expire,
            "role": role,
            "username": username,
            "language": language,
        }
        if token in SESSIONS:
            del SESSIONS[token]
    session["token"] = new_token
    session["expire"] = new_expire
    return session


def calculate(x):
    return float(x) * 2
