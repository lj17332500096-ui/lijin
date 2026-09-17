"""Antragssteller: Antragsteller, der Anträge einreicht."""
APPLICANT_USERS = {"applicant": "app123"}

"""Prüfer: Prüfer, der Anträge prüft und bewertest."""
REVIEWER_USERS = {"reviewer": "rev456"}

"""Administrator: Systemadministrator mit voller Kontrolle."""
ADMIN_USERS = {"admin": "admin789"}

import threading

USERS = {**APPLICANT_USERS, **REVIEWER_USERS, **ADMIN_USERS}

# 持久化所有已创建的 session，key 为 token 字符串
SESSIONS: dict[str, dict] = {}
SESSION_LOCK = threading.Lock()


def login(username, password):
    if username not in USERS or USERS[username] != password:
        return False
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
    import time
    return persisted["expire"] > time.time()


def create_session(username, language="zh-CN"):
    import time
    import hashlib
    role = "user"
    for role_name, role_users in [("applicant", APPLICANT_USERS), ("reviewer", REVIEWER_USERS), ("admin", ADMIN_USERS)]:
        if username in role_users:
            role = role_name
            break
    # 用 SHA-256 对用户名哈希生成唯一 token，避免不同用户共用同一 token
    raw = f"{username}-{time.time()}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    token = f"session-{token_hash}-{username}"
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
    if not persisted:
        return session
    username = persisted.get("username", "unknown")
    language = persisted.get("language", "zh-CN")
    raw = f"{username}-{time.time()}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    new_token = f"session-{token_hash}-{username}"
    new_session = {
        "token": new_token,
        "expire": time.time() + 3600,
        "role": persisted["role"],
        "username": username,
        "language": language,
    }
    with SESSION_LOCK:
        del SESSIONS[token]
        SESSIONS[new_token] = new_session
    session["token"] = new_token
    session["expire"] = new_session["expire"]
    session["language"] = new_session["language"]
    return session


def calculate(x):
    return float(x) * 2
