"""FORGE TUI — 专业 Agent Runtime Console（Textual 8.x）。

布局
----
┌─────────────────────────────────────────────────────────────────┐
│  ◆ FORGE   personal   GPT-5.6   4 tools   ● READY             │  ← StatusHeader
├─────────────────────────────────────────────────────────────────┤
│  MessageLog                                                    │
│                                                                │
│  YOU                                                           │
│  帮我检查当前项目还有哪些问题。                                   │
│                                                                │
│  ┃ RUN  Inspecting repository                                 │
│  │   ├─ ✓ filesystem   scanned 128 files    0.8s             │
│  │   ├─ ✓ github       loaded branch/main   1.2s             │
│  │   └─ ✓ terminal     pytest                 3.8s           │
│  │                                                             │
│  FORGE                                                          │
│  当前主要还有三个问题……                                          │
│  ✓ DONE · 3 tools · 6.4s · run 3F7A                          │
│  ↳ runtime-audit.md                                            │
├─────────────────────────────────────────────────────────────────┤
│  > _                                                          │
│  / commands   ↑↓ history   Ctrl+L clear   Ctrl+Q quit        │
└─────────────────────────────────────────────────────────────────┘

快捷键
------
  Ctrl+Q   退出
  Ctrl+L   清屏
  Ctrl+O   Inspector
  Tab      命令补全（slash 模式）

入口
----
    python main.py --tui
"""
