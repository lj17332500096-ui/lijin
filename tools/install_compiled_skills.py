"""install_compiled_skills.py — 把 compiler 产出的非 Claude 技能，翻译成真正中文可执行
skill.md，并安装到 此刻/NOW（拷到 skills/<slug>/，同名覆盖=用最新；追加到 .env SKILLS）。

运行后需重启 webapp。Claude 专属技能已在编译时排除（academy-guide / claude-api / web-artifacts-builder）。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "agent-skills"
DEST = ROOT / "skills"
ENV = ROOT / ".env"

# 中文可执行指令（用途/触发/核心做法/约束），已去 Claude 产品词，保留核心步骤
ZH: dict[str, list[str]] = {
    "docx": [
        '# docx（Word 文档处理）',
        '## 用途',
        '创建 / 读取 / 编辑 Word 文档（.docx/.dotx）：目录、标题、页码、图片、批注、查找替换、合并文档。',
        '## 触发',
        '用户提到 Word 文档、.docx/.dotx，或要产出 报告/备忘录/信函/模板 等 Word 交付物时使用。',
        '## 核心做法',
        '- **新建**：写一个 Node 脚本用 `docx`（已预装，勿先 `npm install`），`require("docx")` 直接构建并导出。',
        '- **编辑已有**：`unzip x.docx` -> 修改 `word/document.xml` -> 再 `zip` 回（docx-js 不能打开已有文件）。',
        '- **读取内容**：`pandoc -t markdown file.docx` 转纯净文本/Markdown。',
        '- 详细示例/注意事项见资产 `assets/references/*.md`，脚本在 `assets/scripts/`。',
        '## 约束',
        '不要用于 PDF/电子表格/Google Docs/与文档生成无关的编码任务。',
    ],
    "pptx": [
        '# pptx（PowerPoint 演示文稿）',
        '## 用途',
        '创建/读取/编辑 .pptx/.potx：生成演讲 Deck、提取文本、模板(.potx)、备注与批注、拆分/合并幻灯片。',
        '## 触发',
        '用户提到 deck/幻灯片/presentation 或 .pptx/.potx 文件即使用。',
        '## 核心做法',
        '- **新建**：写 `pptxgenjs` 脚本（依赖已就绪，勿先装）。',
        '- **编辑/从模板构建**：`unzip` -> 改 `ppt/slides/slideN.xml` -> 再 `zip`。',
        '- **读取**：`markitdown deck.pptx`（每页一个块）；可视化网格 `python scripts/thumbnail.py deck.pptx`。',
        '- 脚本路径相对技能目录；其余为普通 Python/node/shell。详见 `assets/references/`。',
    ],
    "xlsx": [
        '# xlsx（电子表格）',
        '## 用途',
        '打开/读取/编辑/修复 .xlsx/.xlsm/.xltx/.csv/.tsv（加列、公式、格式化、图表、清洗脏数据），或新建/互转表格。',
        '## 触发',
        '用户以电子表格为主要输入/输出，或引用某表格文件并要对它做处理时使用。',
        '## 核心做法',
        '- **生成/编辑 + 公式/格式**：`openpyxl`（已预装，勿 `pip install`）。',
        '- **批量进出**：`pandas`（`read_excel`/`to_excel`）。',
        '- **快速查看**：`markitdown file.xlsx`（每表一格，无坐标，别用它规划编辑）。',
        '- **读公式+值**：两次 `load_workbook`（一次 data_only 取计算值，一次取公式）。',
        '- 详见 `assets/references/`。',
        '## 约束',
        '交付物必须是表格文件；若主要交付是 Word/HTML/脚本/数据库管道，即使涉及表格也不要用。',
    ],
    "pdf": [
        '# pdf（PDF 处理）',
        '## 用途',
        '读取/提取文本表格、合并/拆分、旋转、加水印、新建、填表单、加密/解密、提取图片、OCR 扫描件。',
        '## 触发',
        '用户提到 .pdf 文件或要产出 PDF 时使用。',
        '## 核心做法',
        '用 Python 库（pypdf/PyMuPDF 等）+ 命令行工具组合完成上述操作；高级功能与示例看 `assets/REFERENCE.md`；'
        '填表单看 `assets/FORMS.md`。先 `python <脚本> --help` 再调用（大脚本当黑盒）。',
    ],
    "canvas-design": [
        '# canvas-design（视觉设计 · PNG/PDF）',
        '## 用途',
        '用设计理念生成 .png/.pdf 视觉作品（海报、艺术、设计、静态画面）。',
        '## 触发',
        '用户要创作海报/艺术/设计/静态视觉物时使用。',
        '## 核心做法',
        '两步走：1) 先产出「设计理念」(.md)；2) 再到 canvas 上落地为 .pdf/.png。围绕 形态/空间/色彩/构图/图形/纹理/少量文字 设定理念；'
        '只输出 .md/.pdf/.png 三种文件。务必输出**原创**作品，不要复制现有艺术家作品以避免侵权；模板/参考见 `assets/`。',
    ],
    "algorithmic-art": [
        '# algorithmic-art（生成式算法艺术）',
        '## 用途',
        '用 p5.js + 种子随机数生成算法艺术：流动场、粒子系统等。',
        '## 触发',
        '用户要用代码/生成式/算法艺术/流动场/粒子做创作时使用。',
        '## 核心做法',
        '两步：1) 产出「算法理念」(.md)；2) 用 p5.js 表达（.html 交互 + .js 算法）。突出 计算过程/涌现行为/数学之美/种子随机。'
        '输出原创算法艺术，勿复制现有艺术家作品。可参考 `assets/templates/` 的生成器。',
    ],
    "frontend-design": [
        '# frontend-design（前端视觉设计）',
        '## 用途',
        '为新建或改造的 UI 给出有主见、非模板化的视觉设计（审美方向、字体、布局）。',
        '## 触发',
        '用户在做前端界面、要好看/不落俗套/有辨识度的设计时使用。',
        '## 核心做法',
        '先搞清主题/产品是什么（brief 未指明就先确认）；再围绕配色、字体、版式做**有明确倾向**的选择，避免默认模板；'
        '必要且合理时敢于做审美冒险。参考 `assets/references/` 的最佳实践样例。',
    ],
    "brand-guidelines": [
        '# brand-guidelines（品牌视觉规范）',
        '## 用途',
        '把一套品牌色与字体规范应用到任意需要统一视觉的产物上。',
        '## 触发',
        '涉及品牌色/风格规范/视觉排版/公司设计标准时使用。',
        '## 核心做法',
        '主色：深 `#141413`（正文/深底）、浅 `#faf9f5`、中灰 `#b0aea5`、浅灰 `#e8e6dc`；强调色：橙 `#d97757`、蓝（见 assets 完整色板）。'
        '按「主色/强调色/灰度/字体」顺序应用；详细色板与字型见 `assets/`。',
    ],
    "theme-factory": [
        '# theme-factory（主题工厂）',
        '## 用途',
        '给幻灯片、文档、HTML 落地页等 artifact 应用成套主题；内置 10 套配色+字体，也可即时生成新主题。',
        '## 触发',
        '用户要套用专业主题/统一风格到产物时使用。',
        '## 核心做法',
        '选一套主题（含色板 hex + 标题/正文字型对），应用到目标 artifact；没有合适的就按色板+字型现场生成。用法步骤与主题清单见 `assets/references/`。',
    ],
    "slack-gif-creator": [
        '# slack-gif-creator（GIF）',
        '## 用途',
        '生成面向 Slack 优化的动画 GIF（大小/帧率/尺寸合适）。',
        '## 触发',
        '用户要做一个 GIF/动图给 Slack/聊天时使用。',
        '## 核心做法',
        '读 `assets/references/` 的说明与脚本；按目标限制调优 帧率/尺寸/颜色数；`python <脚本> --help` 后黑盒调用。',
    ],
    "webapp-testing": [
        '# webapp-testing（本地 web 应用测试）',
        '## 用途',
        '用 Playwright 本地测试脚本：验证前端功能、调试 UI、截图、看浏览器日志。',
        '## 触发',
        '用户要测试/调试本地 web 应用/前端时使用。',
        '## 核心做法',
        '写原生 Python Playwright 脚本；`assets/scripts/with_server.py` 管理服务器生命周期（支持多服务器）。'
        '**总是先 `python <脚本> --help`** 看用法；除非确需定制，否则不要先读源码（脚本很大，会污染上下文，当黑盒调用）。',
    ],
    "skill-creator": [
        '# skill-creator（技能创建与改进）',
        '## 用途',
        '从零创建技能、改进既有技能、度量性能（跑 eval、方差分析、优化触发描述）。',
        '## 触发',
        '用户想创建/优化技能、跑评估/基准时使用。',
        '## 核心做法',
        '1) 明确技能要做什么、怎么做；2) 写草稿；3) 造几个测试提示词跑一遍；4) 帮助用户定性+定量评估结果；5) 迭代优化（含触发描述）。'
        '评估脚本与说明见 `assets/`（scripts/eval、references）。',
    ],
    "discernment-nudge": [
        '# discernment-nudge（回答后的可行下一步）',
        '## 用途',
        '在给出一个成熟的回答/草稿后，**追加一条简短、可执行的提示**，帮用户朝目标再推进一步。',
        '## 触发',
        '用户已获得较完整回答，但仍可被一个下一步进一步推动时使用（非每次必用，要在合适处加）。',
        '## 核心做法',
        '在结尾给一句具体、可行动的下一步（如「要不要我基于这个展开成第二版？」），只提一个有价值的动作，别堆砌。',
    ],
    "doc-coauthoring": [
        '# doc-coauthoring（文档协同写作）',
        '## 用途',
        '与用户**结构化**地协同写文档：计划->草稿->评审->打磨。',
        '## 触发',
        '用户要一起/协作写一份文档/长文时使用。',
        '## 核心做法',
        '先确认目标与读者->给出大纲/计划->共同确认->分块草稿->评审->按反馈打磨；分阶段推进，避免一次性长篇输出。见 `assets/references/`。',
    ],
    "internal-comms": [
        '# internal-comms（内部沟通写作）',
        '## 用途',
        '撰写各类内部沟通：公告、进展更新、备忘、说明。',
        '## 触发',
        '用户要写 内部公告/更新/备忘/说明 等文字时使用。',
        '## 核心做法',
        '按「受众 + 目的 + 关键信息」组织：先说要点，再给背景；语气贴合内部协作；模板与措辞见 `assets/references/`。',
    ],
    "mcp-builder": [
        '# mcp-builder（MCP 服务器构建）',
        '## 用途',
        '构建高质量 MCP（模型上下文协议）服务器：resources / tools / prompts。',
        '## 触发',
        '用户要做一个 MCP 服务器/给 AI 提供工具时使用。',
        '## 核心做法',
        '用 Python SDK 实现 resources（只读数据）、tools（可调用函数）、prompts（模板）；按规范声明 schema 与描述；示例与最佳实践见 `assets/references/`。',
    ],
}

INCLUDE = list(ZH.keys())


def main() -> int:
    if not SRC.exists():
        print("agent-skills/ 不存在，请先运行 tools/skill_compiler.py", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    lines = [l for l in ENV.read_text(encoding="utf-8").splitlines() if l.strip()] if ENV.exists() else []
    cur = ""
    for l in lines:
        if l.startswith("SKILLS="):
            cur = l.split("=", 1)[1].strip()
    cur_set = {x.strip() for x in cur.split(",") if x.strip()}

    for slug in INCLUDE:
        src = SRC / slug
        if not (src / "skill.md").is_file():
            print(f"  [skip] {slug}: agent-skills 下无 skill.md", file=sys.stderr)
            continue
        (src / "skill.md").write_text("\n".join(ZH[slug]) + "\n", encoding="utf-8")
        dest = DEST / slug
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        if slug not in cur_set:
            cur_set.add(slug)
        print(f"  [installed] {slug}")

    new_cur = ",".join(sorted(cur_set))
    new_lines = [l for l in lines if not l.startswith("SKILLS=")]
    new_lines.append("SKILLS=" + new_cur)
    ENV.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print("\nSKILLS =", new_cur)
    print("已复制到 skills/（同名覆盖为最新）。请重启 webapp 使 skills_loader 加载。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
