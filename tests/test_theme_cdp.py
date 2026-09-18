"""主题回归测试（CDP 真实浏览器）：浅色不得出现大面积深色，深色不得出现纯白容器。

运行需要：后端 127.0.0.1:8765 在线 + Edge 可 headless。
无后端时不自动启动（由测试环境保证），失败会给出明确错误。
"""

import json
import random
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9360 + random.randint(0, 100)
HOST = "http://127.0.0.1:8765"
# 被测主题样式属于旧版 Web UI（web/runtime/*）。`/` 入口已切换为 llama-ui 前端，
# 旧 UI 保留在 /runtime 路由，因此主题回归必须显式指向旧 UI 入口。
ENTRY = HOST + "/runtime"


def rgb_brightness(rgb: str) -> float:
    """解析 getComputedStyle 返回的背景色亮度（0–255 均值）。

    同时支持两种格式：
    - 旧格式 `rgb(11, 14, 18)` / `rgba(11, 14, 18, 0.5)`：分量是 0–255 整数
    - 现代格式 `color(srgb 0.937 0.267 0.267 / 0.12)`：CSS 用了 color-mix() 时
      Chrome 会算成这种形式，分量是 0–1 浮点

    不解析后者会让主题回归以 ValueError 崩溃，看起来像"样式坏了"，
    实际是解析器不支持——必须两种都认，否则定位方向会被带偏。
    """
    if "(" not in rgb:
        raise ValueError(f"无法解析颜色值: {rgb!r}")
    inner = rgb.split("(", 1)[1].rsplit(")", 1)[0]
    float_form = rgb.strip().lower().startswith("color(") or "srgb" in inner.lower()
    nums: list[float] = []
    for part in inner.replace("/", " ").replace(",", " ").split():
        try:
            nums.append(float(part))
        except ValueError:
            continue
        if len(nums) == 3:
            break
    if len(nums) < 3:
        raise ValueError(f"无法解析颜色值: {rgb!r}")
    if float_form:
        nums = [v * 255 for v in nums]
    return sum(nums) / 3


def _kill_stale_edge():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"], capture_output=True, timeout=30)
        time.sleep(1)
    except Exception:
        pass


class ThemeCdpSession:
    def __init__(self):
        self.proc = None
        self.ws = None
        self._id = 0

    def __enter__(self):
        _kill_stale_edge()
        profile = Path.home() / "AppData" / "Local" / "Temp" / f"edge_theme_test_{PORT}_{int(time.time())}"
        self.proc = subprocess.Popen(
            [EDGE, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
             f"--user-data-dir={profile}", "--window-size=1440,900", ENTRY],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = False
        for _ in range(25):
            time.sleep(1)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read()
                ok = True
                break
            except Exception:
                pass
        if not ok:
            raise RuntimeError("Edge CDP 未就绪（后端或浏览器不可用）")
        import websocket

        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read().decode())
        page = next((t for t in targets if t["type"] == "page" and "8765" in t.get("url", "")), None)
        if page is None:
            raise RuntimeError("未找到 8765 页面目标")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60,
                                              origin=f"http://127.0.0.1:{PORT}")
        time.sleep(8)
        return self

    def __exit__(self, *a):
        try:
            if self.ws:
                self.ws.close()
        finally:
            if self.proc:
                self.proc.terminate()

    def eval(self, expr, wait=0):
        if wait:
            time.sleep(wait)
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self._id:
                return m.get("result", {}).get("result", {}).get("value")

    def set_theme(self, theme):
        self.eval(f"(localStorage.setItem('forge.theme.main','{theme}'), "
                  f"document.documentElement.setAttribute('data-theme','{theme}'), 1)")
        self.eval("location.reload()")
        time.sleep(8)

    def open_container(self, want_state):
        return self.eval(f"""(async function(){{
          const d = await RT.api.listTasks({{limit:100}});
          const t = d.tasks.find(x => x.stat && x.stat.latest_run && x.stat.latest_run.state==='{want_state}');
          if (!t) return null;
          await RT.workspace.openExisting(t.id);
          return t.id;
        }})()""", wait=2)

    def computed(self, selector, retries=4):
        out = None
        for _ in range(retries):
            out = self.eval(
                f"""(function(){{
                  const el = document.querySelector({json.dumps(selector)});
                  if (!el) return null;
                  const c = getComputedStyle(el);
                  return {{ bg: c.backgroundColor, bgImage: c.backgroundImage.slice(0,120),
                            color: c.color, inline: el.getAttribute('style') }};
                }})()""")
            if out is not None:
                break
            time.sleep(0.8)
        return out


class LightThemeRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = ThemeCdpSession.__enter__(ThemeCdpSession())
        cls.s.set_theme("light")

    @classmethod
    def tearDownClass(cls):
        ThemeCdpSession.__exit__(cls.s, None, None, None)

    def test_home_hero_and_composer_light(self) -> None:
        self.s.eval("(RT.workspace.goHome(),1)", wait=1.5)
        hero = self.s.computed(".home-hero")
        comp = self.s.computed(".composer")
        wrap = self.s.computed(".composer-wrap")
        self.assertIsNotNone(hero)
        self.assertGreater(rgb_brightness(hero["bg"]), 200, "首页 hero 浅色下不应是深色")
        self.assertGreater(rgb_brightness(comp["bg"]), 200, "Composer 浅色下应为浅色 surface")
        self.assertIn("244, 247, 251", wrap["bgImage"], "Composer 外层渐变遮罩应为浅色")

    def test_result_card_light(self) -> None:
        # 新对话流：完成卡（.result-card）只在产生实际结果的项目出现；取任意 completed 项目
        tid = self.s.open_container("completed")
        if tid is None:
            self.skipTest("库中无 completed 容器")
        time.sleep(2.5)
        d = self.s.computed(".result-card, .deliver-card")
        if d is None:
            self.skipTest("该项目无结果卡（普通问答正文流）——浅色正文由 .flow 覆盖检查")
        self.assertGreater(rgb_brightness(d["bg"]), 200, "结果卡浅色下不得再是深黑/深蓝")
        self.assertIsNone(d["inline"], "结果卡不应有 inline style 注入背景")

    def test_flow_light_area(self) -> None:
        # 对话流容器使用主题色（浅色下非深黑）
        self.s.open_container("completed")
        time.sleep(2.5)
        f = self.s.computed(".flow")
        if f is None:
            self.skipTest("无对话流容器")
        self.assertEqual(f["bg"], "rgba(0, 0, 0, 0)", "对话流不应带自设深色背景")

    def test_fail_card_light(self) -> None:
        tid = self.s.open_container("failed")
        if tid is None:
            self.skipTest("库中无 failed 容器")
        time.sleep(2)
        f = self.s.computed(".fail-card")
        self.assertIsNotNone(f)
        self.assertGreater(rgb_brightness(f["bg"]), 200, "失败卡浅色下应为浅色背景")

    def test_settings_projectlist_light(self) -> None:
        self.s.eval("(RT.workspace.goHome(),1)", wait=1.5)
        self.s.eval("(document.querySelector('#accountBtn').click(),1)", wait=0.5)
        self.s.eval("(document.querySelector(\"#accountMenu .am-item[data-act=settings]\").click(),1)", wait=1.5)
        sec = self.s.computed(".settings .tl-sec")
        self.assertIsNotNone(sec)
        self.assertGreater(rgb_brightness(sec["bg"]), 200, "设置页容器浅色下应为白色")
        self.s.eval("(RT.workspace.showProjects(),1)", wait=1.5)
        sec3 = self.s.computed(".tasklist .tl-sec")
        self.assertIsNotNone(sec3)
        self.assertGreater(rgb_brightness(sec3["bg"]), 200, "项目列表浅色下应为白色")


class DarkThemeRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = ThemeCdpSession.__enter__(ThemeCdpSession())
        cls.s.set_theme("dark")

    @classmethod
    def tearDownClass(cls):
        ThemeCdpSession.__exit__(cls.s, None, None, None)

    def test_composer_dark_not_white(self) -> None:
        comp = self.s.computed(".composer")
        self.assertIsNotNone(comp)
        self.assertLess(rgb_brightness(comp["bg"]), 60, "深色主题下 Composer 不应变成纯白容器")

    def test_result_card_dark(self) -> None:
        tid = self.s.open_container("completed")
        if tid is None:
            self.skipTest("库中无 completed 容器")
        time.sleep(2.5)
        d = self.s.computed(".result-card, .deliver-card")
        if d is None:
            self.skipTest("该项目无结果卡")
        self.assertLess(rgb_brightness(d["bg"]), 60, "深色主题下结果卡应保持深色")

    def test_wrap_gradient_dark(self) -> None:
        wrap = self.s.computed(".composer-wrap")
        self.assertIn("11, 14, 18", wrap["bgImage"], "深色主题下 Composer 外层渐变应为深色遮罩")


if __name__ == "__main__":
    unittest.main(verbosity=2)
