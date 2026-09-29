# 旧版 Web UI 验收归档

这些截图、DOM 快照和一次性探针对应已退役的 `web/runtime/` 页面，不属于当前 Web 应用或活动测试集。
当前冻结网页宿主只挂载 `web/llama-ui/` 构建产物与 OpenAI 兼容桥；旧 UI 代码不从此处加载。

目录保留原始批次名称，便于对照历史验收结果：

- `visual_audit_20260907/`
- `ui_round1_20260908/`
- `brand_system_20260908/`
- `settings_style_20260908/`
- `index.html.legacy.archive`：旧网页卡片渲染器的 HTML 快照
- `contract_probe.js`、`frontend_smoke.js`、`remove_model_selector.py`、`test_contract.py`、`test_theme_cdp.py`、`test_frontend_smoke.py`：旧前端专用探针或失效测试

这些归档材料仅供历史追溯。不要从此处运行探针，也不要把它们当作当前 UI 验收结果。
