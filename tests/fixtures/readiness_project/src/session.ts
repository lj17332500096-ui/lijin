// 会话存储（fixture）
// 已知缺陷：token 只保存在内存，刷新页面后丢失 → 表现为“登录后刷新页面会退出”。

export class SessionStore {
  private static instance: SessionStore | null = null;

  private tokenKey = "readiness_app_token";

  static get(): SessionStore {
    if (!SessionStore.instance) {
      SessionStore.instance = new SessionStore();
    }
    return SessionStore.instance;
  }

  saveToken(token: string): void {
    // FIXME(known-bug): 刷新后丢失，应改用 sessionStorage
    localStorage.setItem(this.tokenKey, token);
  }

  loadToken(): string | null {
    return localStorage.getItem(this.tokenKey);
  }

  clear(): void {
    localStorage.removeItem(this.tokenKey);
  }
}
