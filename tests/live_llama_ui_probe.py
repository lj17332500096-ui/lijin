# -*- coding: utf-8 -*-
"""llama-ui 真实浏览器探针（CDP）——定位「frontend 相对路径 + SPA 子路由」类故障。

用途
----
后端 `webapp.py` 在线（默认 127.0.0.1:8765）时运行本脚本，它会：

1. 起一个 headless Edge，打开 http://127.0.0.1:8765/llama-ui/
2. 抓取 Network / Console / Log 事件
3. 在 UI 输入框里发一条消息，等待回答
4. 打印所有请求 URL（尤其是 4xx/5xx）与 `location.href` 变化

背景（为什么会需要它）
----------------------
llama-ui 是 SvelteKit 路径路由 SPA，路由表为：

    {"/(chat)":…, "/(chat)/chat/[id]":…, "/mcp-servers":…, "/search":…, "/settings/[[section]]":…}

而它的后端接口全部写成**相对路径**：

    fH  = {COMPLETIONS:"./v1/chat/completions", CONTROL:"./v1/chat/completions/control"}
    V1e = {BASE:"./v1/stream", LOOKUP:"./v1/streams/lookup"}
    H1e = {LIST:"./slots"}

相对路径按 `document.baseURI` 解析。一旦 SPA 跳到 `/llama-ui/chat/<id>`，
`./v1/chat/completions` 就变成 `/llama-ui/chat/v1/chat/completions` —— 后端没有
对应路由，前端就会报 “Unexpected token '<'”（拿到 HTML）或
“Stream connection lost and could not be resumed”（拿到非 200）。

用法
----
    .venv/Scripts/python.exe tests/live_llama_ui_probe.py
    .venv/Scripts/python.exe tests/live_llama_ui_probe.py "自定义提问"

需要后端已启动（本脚本不自动拉起服务）。
"""

import json
import random
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
HOST = "http://127.0.0.1:8765"
ENTRY = HOST + "/llama-ui/"
PORT = 9500 + random.randint(0, 300)

MESSAGE = " ".join(sys.argv[1:]) or "你好，请用一句话介绍你自己"


class Probe:
    def __init__(self) -> None:
        self.proc = None
        self.ws = None
        self._id = 0
        self.requests: list[dict] = []
        self.console: list[str] = []

    # ---- lifecycle ----
    def start(self) -> None:
        profile = Path.home() / "AppData" / "Local" / "Temp" / f"edge_probe_{PORT}_{int(time.time())}"
        self.proc = subprocess.Popen(
            [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", f"--remote-debugging-port={PORT}",
             "--remote-allow-origins=*", f"--user-data-dir={profile}",
             "--window-size=1440,900", ENTRY],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(1)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read()
                break
            except Exception:
                pass
        else:
            raise RuntimeError("Edge CDP 未就绪")
        import websocket

        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read().decode())
        page = next((t for t in targets if t["type"] == "page" and "8765" in t.get("url", "")), None)
        if page is None:
            raise RuntimeError(f"未找到 8765 页面目标: {[t.get('url') for t in targets]}")
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=180, origin=f"http://127.0.0.1:{PORT}")
        for domain in ("Network.enable", "Runtime.enable", "Log.enable"):
            self.send(domain)
        time.sleep(6)

    def stop(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        finally:
            if self.proc:
                self.proc.terminate()

    # ---- cdp ----
    def send(self, method: str, params: dict | None = None) -> int:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return self._id

    def eval(self, expr: str, wait: float = 0.0):
        if wait:
            time.sleep(wait)
        rid = self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})
        return self._recv_result(rid)

    def _recv_result(self, rid: int):
        while True:
            m = json.loads(self.ws.recv())
            self._absorb(m)
            if m.get("id") == rid:
                return (m.get("result", {}).get("result", {}) or {}).get("value")

    def pump(self, seconds: float) -> None:
        """持续读取事件（含 Network 事件），持续 seconds 秒。"""
        end = time.time() + seconds
        self.ws.settimeout(1.0)
        while time.time() < end:
            try:
                self._absorb(json.loads(self.ws.recv()))
            except Exception:
                pass
        self.ws.settimeout(180)

    def _absorb(self, m: dict) -> None:
        method = m.get("method")
        p = m.get("params") or {}
        if method == "Network.requestWillBeSent":
            req = p.get("request", {}) or {}
            self.requests.append({"url": req.get("url", ""),
                                  "method": req.get("method", ""),
                                  "headers": req.get("headers") or {},
                                  "status": None})
        elif method == "Network.responseReceived":
            url = p.get("response", {}).get("url", "")
            for r in reversed(self.requests):
                if r["url"] == url and r["status"] is None:
                    r["status"] = p.get("response", {}).get("status")
                    r["ct"] = (p.get("response", {}).get("headers") or {}).get("Content-Type") \
                        or (p.get("response", {}).get("headers") or {}).get("content-type")
                    break
        elif method == "Network.loadingFailed":
            self.console.append(
                f"[net-fail] {str(p.get('errorText'))[:80]} "
                f"type={p.get('type')} canceled={p.get('canceled')} "
                f"req={p.get('requestId')}")
        elif method == "Runtime.consoleAPICalled":
            args = p.get("args") or []
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            if text.strip():
                self.console.append(f"[{p.get('type')}] {text[:300]}")
        elif method == "Log.entryAdded":
            e = p.get("entry") or {}
            if e.get("text"):
                self.console.append(f"[log:{e.get('level')}] {str(e.get('text'))[:300]}")
        elif method == "Runtime.exceptionThrown":
            d = (p.get("exceptionDetails") or {})
            self.console.append(f"[exception] {str(d.get('text'))[:200]} "
                                f"{str((d.get('exception') or {}).get('description'))[:300]}")

    # ---- ui ----
    def send_message(self, text: str) -> bool:
        """把 text 填进输入框并回车（尽量兼容多种 DOM 形态）。"""
        script = """
        (function(txt){
          const pick = [
            'textarea',
            'input[type=text]',
            '[contenteditable=true]',
            '.composer textarea',
            '.composer-wrap textarea',
          ];
          let el = null;
          for (const sel of pick) { el = document.querySelector(sel); if (el) break; }
          if (!el) return {ok:false, reason:'no-input', html:document.body.innerHTML.slice(0,200)};
          el.focus();
          if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
            const setter = Object.getOwnPropertyDescriptor(
              el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,
              'value').set;
            setter.call(el, txt);
          } else {
            el.textContent = txt;
          }
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          const opts = {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true,
                        cancelable:true, composed:true};
          el.dispatchEvent(new KeyboardEvent('keydown', opts));
          el.dispatchEvent(new KeyboardEvent('keypress', opts));
          el.dispatchEvent(new KeyboardEvent('keyup', opts));
          return {ok:true, tag:el.tagName, cls:el.className};
        })(%s)
        """ % json.dumps(text)
        return bool(self.eval(script))

    def href(self) -> str:
        return self.eval("location.href")

    def force_visibility_event(self) -> None:
        """伪造一次「页面变为可见」的 visibilitychange。

        前端 handleStreamResponse 里注册了：

            const P = () => {
              document.visibilityState === "visible"
                && (N || (d && Date.now() - S > 3000))
                && g.cancel().catch(()=>{});
            };
            document.addEventListener("visibilitychange", P);

        即：页面重新可见、且距上次收到字节已超 3s 时，前端会**主动 cancel 掉
        reader**，然后转入 stream resume 分支。真实用户切窗口/点回浏览器就会
        触发——headless 探针默认不会，所以必须显式伪造。
        """
        self.eval("""(function(){
          try {
            Object.defineProperty(document, 'visibilityState', {configurable:true, get:()=> 'visible'});
            Object.defineProperty(document, 'hidden', {configurable:true, get:()=> false});
          } catch (e) {}
          document.dispatchEvent(new Event('visibilitychange'));
          return document.visibilityState;
        })()""")

    def visible_error(self) -> str:
        return self.eval(
            "(function(){const b=document.body.innerText||'';"
            "return b.replace(/\\s+/g,' ').slice(0,600);})()")


def main() -> int:
    p = Probe()
    try:
        p.start()
        print("=== 打开后 location ===", p.href())
        print("=== 发送前 body 片段 ===", (p.visible_error() or "")[:200])

        rounds = [MESSAGE, "那 2+3 呢？只回答数字"]
        storm = "--no-visibility" not in sys.argv
        for idx, msg in enumerate(rounds, 1):
            print(f"\n=== 第 {idx} 轮 发送: {msg!r}")
            sent = p.send_message(msg)
            print("    输入/回车注入结果 =", sent)
            if storm:
                # 生成期间反复伪造「页面重新可见」：距上次字节 >3s 时前端会 cancel 流
                print("    [模拟切窗口/切回：周期性触发 visibilitychange]")
                deadline = time.time() + 22
                while time.time() < deadline:
                    p.pump(1.5)
                    p.force_visibility_event()
            p.pump(45)
            print("    location =", p.href())
            print("    页面文本 =", (p.visible_error() or "")[:400])

        print("\n=== 网络请求（仅显示非静态资源 / 非 200）===")
        for r in p.requests:
            url = r["url"]
            if any(url.endswith(s) for s in (".png", ".ico", ".svg", ".css", ".webmanifest")):
                continue
            if "/_app/" in url:
                continue
            bad = r["status"] is None or (r["status"] and r["status"] >= 400)
            mark = "  <<<" if bad else ""
            print(f"  {r['method']:5} {r['status']} {url}{mark}")
        print("\n=== Console / 异常 / 网络失败 ===")
        for line in p.console[:60]:
            print("  ", line)
        print("\n=== 关键 URL 解析自检（在页面内用相对路径解析）===")
        print(p.eval("""(function(){
          const rel = ['./v1/chat/completions','./v1/stream','./props','./slots','./v1/streams/lookup'];
          const out = {baseURI: document.baseURI, href: location.href};
          out.resolved = {};
          for (const r of rel) out.resolved[r] = new URL(r, document.baseURI).href;
          return out;
        })()"""))
        print("\n=== 聊天/续流请求的请求头（定位会话标识）===")
        for r in p.requests:
            if any(k in r["url"] for k in ("chat/completions", "/v1/stream", "streams/lookup")):
                hs = {k.lower(): v for k, v in (r.get("headers") or {}).items()
                      if k.lower().startswith("x-") or k.lower() == "content-type"}
                print(f"  {r['method']:5} {r['status']} {r['url'][:110]}  headers={hs}")
        return 0
    finally:
        p.stop()


if __name__ == "__main__":
    # 前端 UI 已冻结（只优化后端阶段）：本探针针对 llama-ui，默认拒绝运行。
    # 恢复：set FORGE_ENABLE_UI=1（同时需要用 FORGE_ENABLE_UI 起 webapp.py）。
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from ui_frozen import frozen_notice, ui_enabled

    if not ui_enabled(_sys.argv[1:]):
        print(frozen_notice())
        print("\n（tests/live_llama_ui_probe.py 是针对前端 UI 的探针，随 UI 一并冻结。）")
        raise SystemExit(2)
    raise SystemExit(main())
