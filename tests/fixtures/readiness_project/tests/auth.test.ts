// 认证逻辑 jest 用例（fixture）

import { AuthClient } from "../src/auth";

describe("AuthClient", () => {
  it("login stores token for later refresh", async () => {
    const client = new AuthClient();
    global.fetch = jest.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ token: "t1", user: { id: "1", name: "tester" } }),
    });
    const result = await client.login("tester", "secret");
    expect(result.token).toBe("t1");
    expect(client.refresh).toBeDefined();
  });

  it("throws when not logged in", async () => {
    const client = new AuthClient();
    await expect(client.refresh()).rejects.toThrow("not logged in");
  });
});
