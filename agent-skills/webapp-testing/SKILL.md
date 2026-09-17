# webapp-testing（本地 web 应用测试）
## 用途
用 Playwright 本地测试脚本：验证前端功能、调试 UI、截图、看浏览器日志。
## 触发
用户要测试/调试本地 web 应用/前端时使用。
## 核心做法
写原生 Python Playwright 脚本；`assets/scripts/with_server.py` 管理服务器生命周期（支持多服务器）。**总是先 `python <脚本> --help`** 看用法；除非确需定制，否则不要先读源码（脚本很大，会污染上下文，当黑盒调用）。
