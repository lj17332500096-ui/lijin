APPLICANT_USERS = {"applicant": "app123"}

# 无认证用户/访客角色
GUEST_USERS = {"guest": "guest123"}

"""Prüfer: Prüfer, der Anträge prüft und bewertest."""
REVIEWER_USERS = {"reviewer": "rev456"}

"""Administrator: Systemadministrator mit voller Kontrolle."""
ADMIN_USERS = {"admin": "admin789"}

import threading
import time
import hashlib
import uuid

USERS = {**APPLICANT_USERS, **REVIEWER_USERS, **ADMIN_USERS, **GUEST_USERS}

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
    """验证用户名密码，成功后自动创建 session 并返回 session 字典，失败返回 None。"""
    if username not in USERS or USERS[username] != password:
        return None
    session = create_session(username)
    return session


def is_logged_in(session):
    """通过 token 查持久化 session，判断是否仍在有效期。"""
    if not session or "token" not in session:
        return False
    # 优先检查持久化 session
    token = session["token"]
    with SESSION_LOCK:
        persisted = SESSIONS.get(token)
    if persisted:
        return persisted["expire"] > time.time()
    # 兜底：如果 session 不在持久化存储中，直接使用 session 自身的 expire
    return session.get("expire", 0) > time.time()


def create_session(username, language="zh-CN"):
    """创建新 session，返回包含 token 等信息的字典。"""
    role = "guest"
    for role_name, role_users in [("guest", GUEST_USERS), ("applicant", APPLICANT_USERS), ("reviewer", REVIEWER_USERS), ("admin", ADMIN_USERS)]:
        if username in role_users:
            role = role_name
            break
    # 用 SHA-256 对用户名和时间戳哈希生成唯一 token
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
    """获取登录用户的角色。"""
    if is_logged_in(session):
        token = session.get("token")
        with SESSION_LOCK:
            persisted = SESSIONS.get(token)
        if persisted:
            return persisted.get("role", "user")
    return None


def refresh_token(session):
    """刷新 token，延长过期时间，保持用户信息不变。"""
    if not session or "token" not in session:
        return session
    token = session.get("token")
    with SESSION_LOCK:
        persisted = SESSIONS.get(token)

    if persisted:
        username = persisted.get("username", "unknown")
        language = persisted.get("language", "zh-CN")
        role = persisted.get("role", "user")
    else:
        # 兜底：从 session 参数获取信息
        username = session.get("username", "unknown")
        language = session.get("language", "zh-CN")
        role = session.get("role", "user")

    raw = f"{username}-{time.time()}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    new_token = f"session-{token_hash}-{username}"
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


def get_language(session):
    """获取用户语言，同时刷新 token 有效期。"""
    if not session or "token" not in session:
        return None

    token = session.get("token")
    with SESSION_LOCK:
        persisted = SESSIONS.get(token)

    if not persisted:
        return None  # token 无效，未登录

    username = persisted.get("username", "unknown")
    language = persisted.get("language", "zh-CN")
    role = persisted.get("role", "user")

    raw = f"{username}-{time.time()}"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
    new_token = f"session-{token_hash}-{username}"
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
    return language


def calculate(x):
    """支持整数、浮点数、小数字符串等输入，返回 x * 2"""
    try:
        return float(x) * 2
    except (TypeError, ValueError):
        return 0.0
