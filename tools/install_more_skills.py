"""install_more_skills.py — 追加安装：summarize-meeting / weekly-review / resume-tailor。
转成中文可执行指令（去 Claude/Copilot 词、去 python-tools 硬依赖，改用内置能力），
拷贝可选资产到 assets/，写入 skills/<slug>/skill.md + SKILL.portable.md，并追加 .env SKILLS。
运行后需重启 webapp。
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

SKILL_ZIP = Path("F:/Byong-hermes/skills/Claude-Skills-main.zip")
PROJ = Path(__file__).resolve().parent.parent
DEST = PROJ / "skills"
ENV = PROJ / ".env"

SOURCES = {
    "summarize-meeting": "Claude-Skills-main/project-management/execution/summarize-meeting",
    "weekly-review": "Claude-Skills-main/personal-productivity/weekly-review",
    "resume-tailor": "Claude-Skills-main/personal-productivity/resume-tailor",
}

ZH = {
    "summarize-meeting": [
        "# summarize-meeting（会议总结）",
        "## 用途",
        "把一场会议的内容（记录/转写/对话）整理成**结构化会议纪要**：议题、决策、行动项、待确认问题，格式统一。",
        "## 触发",
        "用户提供会议记录/转写，或要求「总结这个会议 / 出一份会议纪要 / 提取决议和待办」时使用。",
        "## 核心做法",
        "1) 若无现成记录，先明确 参会人 / 议题 / 时长。",
        "2) 输出四块：**决策**（已拍板）、**行动项**（每项：做什么 / 负责人 / 截止）、**待确认**（悬而未决）、**关键讨论**（简短）。",
        "3) 行动项用列表 + 负责人 + 截止；决策用一句「确定…」。",
        "4) 用一致的 Markdown 格式，便于归档。",
        "## 约束", "只依据记录；没有明确写出的不要脑补；不确定的归入「待确认」。",
    ],
    "weekly-review": [
        "# weekly-review（周复盘）",
        "## 用途",
        "把一周的输入（日程、任务、笔记、OKR/目标检查）综合成一份**周复盘**：成果、收获、卡点、下周优先事项。",
        "## 触发",
        "周五/周日复盘、GTD 周检、OKR 检查；用户要求「做一次周复盘 / 总结这周 / 下周计划」时使用。",
        "## 核心做法",
        "1) 收集本周素材（从对话/笔记/任务/日程里找，必要时请用户补充）。",
        "2) 输出：✅ 本周成果、💡 学到/值得保留、⚠️ 卡点（阻拦与原因）、🎯 下周 3 项重点。",
        "3) 若用户有目标/OKR，对照进展；没有就聚焦「完成的事 / 没完成的事 / 原因」。",
        "4) 复盘要有行动导向：每个卡点配一个「下一步动作」。",
        "## 约束", "只依据本周实际发生；不要编造成果；未定项标注「待确认」。",
    ],
    "resume-tailor": [
        "# resume-tailor（简历定制）",
        "## 用途",
        "针对一个职位要求（JD），定制简历：提炼关键词、评估匹配度、重写经历描述以提升冲击力（并兼顾 ATS）。",
        "## 触发",
        "用户要「针对某岗位改简历 / 优化简历匹配 / 写求职信 / 过 ATS 关键词」时使用。",
        "## 核心做法",
        "1) 先读 JD，提取关键词（技能、工具、年限、软技能），列出匹配/缺失。",
        "2) 对照简历逐条评估，给出「匹配度 + 缺哪些 + 可强调哪些」。",
        "3) 重写经历 bullet：用 动词+成果(量化) 句式；把 JD 关键词自然融入（不堆砌）。",
        "4) 若写求职信，用简短一页，突出与岗位的契合。",
        "5) 说明每处改动，标注「可再确认」项。",
        "## 约束", "**真实**：不改事实、不夸大成未真实发生的；关键词自然融入而非机械堆砌。",
    ],
}


def copy_assets(zf: zipfile.ZipFile, root: str, dest: Path) -> list[str]:
    prefix = root + "/"
    dirs = set()
    for n in zf.namelist():
        if n.startswith(prefix) and "/" in n[len(prefix):]:
            top = n[len(prefix):].split("/", 1)[0]
            dirs.add(top)
    copied = []
    for top in dirs:
        if top in ("skills", ".github", "LICENSE", "LICENSE.txt", "NOTICE", "README.md"):
            continue
        for n in zf.namelist():
            if not n.startswith(prefix):
                continue
            rel = n[len(prefix):]
            if rel == top or not rel.startswith(top + "/"):
                continue
            out = dest / "assets" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            if not n.endswith("/"):
                out.write_bytes(zf.read(n))
        copied.append(top)
    return copied


def main() -> int:
    zf = zipfile.ZipFile(SKILL_ZIP)
    DEST.mkdir(parents=True, exist_ok=True)
    env_lines = [l for l in ENV.read_text(encoding="utf-8").splitlines() if l.strip()] if ENV.exists() else []
    cur = ""
    for l in env_lines:
        if l.startswith("SKILLS="):
            cur = l.split("=", 1)[1].strip()
    cur_set = {x.strip() for x in cur.split(",") if x.strip()}

    for slug, root in SOURCES.items():
        md = ""
        for c in (f"{root}/SKILL.md", f"{root}/skill.md"):
            if c in zf.namelist():
                md = zf.read(c).decode("utf-8", "replace")
                break
        d = DEST / slug
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        (d / "skill.md").write_text("\n".join(ZH[slug]) + "\n", encoding="utf-8")
        if md:
            (d / "SKILL.portable.md").write_text(md, encoding="utf-8")
        assets = copy_assets(zf, root, d)
        if slug not in cur_set:
            cur_set.add(slug)
        print(f"  [installed] {slug}  (assets: {','.join(assets) if assets else 'none'})")
    zf.close()

    env_out = [l for l in env_lines if not l.startswith("SKILLS=")]
    env_out.append("SKILLS=" + ",".join(sorted(cur_set)))
    ENV.write_text("\n".join(env_out) + "\n", encoding="utf-8")
    print("\nSKILLS =", ",".join(sorted(cur_set)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
