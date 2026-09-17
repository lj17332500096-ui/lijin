// 登录与 token 刷新逻辑（fixture）

export interface LoginResult {
  token: string;
  user: { id: string; name: string };
}

export class AuthClient {
  private token: string | null = null;

  async login(username: string, password: string): Promise<LoginResult> {
    const resp = await fetch("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      throw new Error(`login failed: ${resp.status}`);
    }
    const data = (await resp.json()) as LoginResult;
    this.token = data.token;
    return data;
  }

  async refresh(): Promise<string> {
    if (!this.token) {
      throw new Error("not logged in");
    }
    const resp = await fetch("/api/refresh", {
      method: "POST",
      headers: { Authorization: `Bearer ${this.token}` },
    });
    if (resp.status === 401) {
      throw new Error("session expired");
    }
    if (!resp.ok) {
      throw new Error(`refresh failed: ${resp.status}`);
    }
    const data = (await resp.json()) as { token: string };
    this.token = data.token;
    return data.token;
  }
}
