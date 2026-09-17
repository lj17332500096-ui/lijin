"""install_daily_skills.py — 从 F:/Byong-hermes/skills 的各 zip 抽取推荐技能，
翻译/改写成贴合「此刻/NOW」的中文可执行 skill.md，并安装到 skills/<slug>/（含资产）
+ 追加 .env SKILLS。运行后需重启 webapp。

已**去耦合**：Granola/WorkIQ/bun-npx/已归档的 pdf-pptx-docx 技能/特定 WORKSPACE 文件夹等，
改用内置工具（read_office_file / read_spreadsheet 等）。
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

SKILL_ZIP_DIR = Path("F:/Byong-hermes/skills")
PROJ = Path(__file__).resolve().parent.parent
DEST = PROJ / "skills"
ENV = PROJ / ".env"

# slug -> (zip, 源根路径), None 源根=zip 顶层
SOURCES: dict[str, tuple[str, str | None]] = {
    "baoyu-translate": ("baoyu-translate-1.117.3.zip", None),
    "note-organizer": ("note-organizer-main.zip", "note-organizer-main"),
    "daily-plan": ("ai-agent-knowledge-bank-main.zip", "ai-agent-knowledge-bank-main/skills/daily-plan"),
    "daily-summary": ("ai-agent-knowledge-bank-main.zip", "ai-agent-knowledge-bank-main/skills/daily-summary"),
    "summarise-session": ("ai-agent-knowledge-bank-main.zip", "ai-agent-knowledge-bank-main/skills/summarise-session"),
    "simplify": ("ai-agent-knowledge-bank-main.zip", "ai-agent-knowledge-bank-main/skills/simplify"),
    "email-drafter": ("awesome-copilot-main.zip", "awesome-copilot-main/skills/email-drafter"),
    "content-creator": ("Claude-Skills-main.zip", "Claude-Skills-main/marketing/content-creator"),
}

ZH: dict[str, list[str]] = {
    "baoyu-translate": [
        "# baoyu-translate（三模式翻译）",
        "## 用途", "高质量中英互译 / 本地化。三种模式：快速（直译）、标准（结合语境/术语）、精细（出版级，含审校润色）。",
        "## 触发", "用户要求 翻译 / 译成中文 / 译成英文 / 本地化 / 润色译文 / 校对翻译，或给 URL/文件要求翻译。",
        "## 核心做法",
        "- **快速**：直接给忠实译文，保留原文格式（标题/列表/粗体）。",
        "- **标准**：先分析语气与领域术语，再翻译，并给出关键术语词汇表。",
        "- **精细**：翻译 -> 自我审校（易读性/术语一致/语气）-> 润色 -> 输出最终版 + 修订说明。",
        "- 支持自定义术语表：用户指定/已给出的术语固定不译。",
        "## 约束", "忠实原文、术语一致、保留 Markdown 结构；不要随意改写原意。",
    ],
    "note-organizer": [
        "# note-organizer（笔记/资料整理）",
        "## 用途",
        "把零散的学习/课程/会议资料（PDF/Word/Excel/PPT、讲稿、课本、个人笔记、复习题）整理成结构化的 Markdown 知识库。",
        "## 触发",
        "用户提到「整理这些资料/笔记/课件/讲义/复习材料」「把知识点整理出来」时使用。",
        "## 核心做法",
        "1) 先盘点现有材料（用内置 `read_office_file` / `read_spreadsheet` 读取 PDF/Word/Excel/PPT 内容）。",
        "2) 生成材料清单与来源，标记不确定项。",
        "3) 按章节/主题推断结构，按资料类型用对应笔记模板。",
        "4) 为每份材料产出：章节笔记 + 来源 + 不确定项 + 知识点。",
        "5) 建立索引（标题/wikilinks），做一致性检查，必要时生成练习题（含答案/解析/来源）。",
        "## 约束", "忠于来源，标注「不确定/待补」；不要编造内容；默认用用户语言并保留课程术语。",
    ],
    "daily-plan": [
        "# daily-plan（每日计划）",
        "## 用途", "规划「今天/下一个工作日」的待办清单：结转未完成项、合并要做的事、按优先级排布。",
        "## 触发", "用户要求「列一下今天的待办 / 明天计划 / 接下来要做的事 / 帮我规划今天」时使用。",
        "## 核心做法",
        "1) 先看今天/昨天还没完成的待办（从对话或用户提供的清单里识别）。",
        "2) 汇总「会议/沟通里要跟进」的事。",
        "3) 按 重要+紧急 排序，给出 3~5 个重点 + 时间块建议，输出为可复制清单。",
        "4) 不用反复打断，给出可直接改的版本并说明取舍。",
        "## 约束", "不臆造任务；不确定的用「待确认」标注。",
    ],
    "daily-summary": [
        "# daily-summary（每日总结）",
        "## 用途", "把「今天的会议/对话/记录」整理成带日期的每日总结：要点、行动项、责任人、状态。",
        "## 触发", "用户要求「总结今天的会议 / 写今日总结 / 把今天的记录整理一下」时使用。",
        "## 核心做法",
        "1) 收集今天相关的内容（用户提供的会议记录/笔记/对话）。",
        "2) 逐条：摘要要点 + 提取行动项（做什么/谁/截止）+ 记录决策。",
        "3) 输出结构化 Markdown（日期 + 要点 + 待办 + 风险/待确认）。",
        "## 约束", "只依据已有内容；没有就如实说明；不过度脑补。",
    ],
    "summarise-session": [
        "# summarise-session（会话/工作小结）",
        "## 用途", "把当前对话/一段工作整理成结构化小结，并写入工作区的状态文档。",
        "## 触发", "用户要求「总结一下这次会话 / 把今天做了什么记下来 / 更新工作小结」时使用。",
        "## 核心做法",
        "1) 回顾会话做了什么、有哪些决定、遇到什么问题。",
        "2) 形成：目标 / 已完成 / 进行中 / 未决问题 / 下一步。",
        "3) 写入工作区（如 `docs/status_docs/` 或用户指定位置）的 `WORK_SUMMARY_<日期>.md`。",
        "## 约束", "忠于事实，只总结不新增；标注未决项。",
    ],
    "simplify": [
        "# simplify（代码简化）",
        "## 用途", "在不改功能的前提下，把刚改过的代码变得更清晰、一致、易维护。",
        "## 触发", "写完/改完代码后，用户要求「简化一下 / 让代码更清晰 / 提升可读性 / 重构」时使用。",
        "## 核心做法",
        "1) 保持功能与行为完全不变，只改「怎么写」。",
        "2) 减少嵌套与重复、去掉冗余抽象；用清晰命名；合并相关逻辑；删除对明显代码的解释性注释。",
        "3) 可读性优先于「短」；避免过度简化破坏边界。",
        "4) 说明每处改动，并提示用测试/运行确认行为未变。",
        "## 约束", "绝不改变行为/输出/副作用。",
    ],
    "email-drafter": [
        "# email-drafter（邮件草稿）",
        "## 用途", "写/回复/跟进专业邮件，语气、称呼、结构、结尾贴合对象与场景。",
        "## 触发", "用户要求「给 XX 写封邮件 / 回复 / 跟进邮件 / 邮件语气」时使用。",
        "## 核心做法",
        "1) 先确认：收件人与关系、目的、关键信息、期望动作。",
        "2) 按「称呼 -> 开场 -> 要点（分点）-> 明确行动 -> 结尾/签名」结构；语气随对象（友好/正式/坚定/委婉）。",
        "3) 若已有往来历史，参考其语气保持一致。",
        "4) 给出可审阅的草稿，标注可选改动。",
        "## 约束", "尊重收件人关系；避免生硬/过度客套；不虚构对方已承诺的事。",
    ],
    "content-creator": [
        "# content-creator（内容创作/品牌声音+SEO）",
        "## 用途", "产出与品牌声音一致、经 SEO 优化的营销内容：博客、社媒、落地页、文案；含品牌声音分析与内容框架。",
        "## 触发", "用户要求「写一篇博客/公众号/小红书/社媒内容/落地页文案 / 分析品牌声音 / SEO 优化」时使用。",
        "## 核心做法",
        "1) 先分析/确认品牌声音（语气词、词汇、受众），写作全程保持一致。",
        "2) 用内容框架组织：钩子 -> 主体价值 -> 行动号召。",
        "3) SEO：关键词、标题/描述、结构化标题(H1/H2/H3)、内部链接、可读性。",
        "4) 按平台调整长度与风格（博客/社媒/落地页不同）。",
        "5) 需要品牌/SEO 脚本时，用 `read_skill_asset` 读取 `assets/` 下的分析脚本参考。",
        "## 约束", "忠于品牌声音；不堆砌关键词；不传播未核实的事实。",
    ],
}


def _open_zip(zip_name: str):
    p = SKILL_ZIP_DIR / zip_name
    if not p.is_file():
        return None
    return zipfile.ZipFile(p)


def extract_skill_md(zf: zipfile.ZipFile, root: str | None) -> str:
    cands = [f"{root}/SKILL.md" if root else "SKILL.md",
             f"{root}/skill.md" if root else "skill.md"]
    for c in cands:
        if c in zf.namelist():
            return zf.read(c).decode("utf-8", "replace")
    return ""


def copy_assets(zf: zipfile.ZipFile, root: str | None, dest: Path) -> list[str]:
    """把技能目录下的子目录（references/scripts/templates/fonts 等）拷到 dest/assets/。"""
    copied: list[str] = []
    prefix = (root + "/") if root else ""
    dirs = set()
    for n in zf.namelist():
        if prefix and not n.startswith(prefix):
            continue
        rel = n[len(prefix):]
        if "/" not in rel:
            continue
        top = rel.split("/", 1)[0]
        dirs.add(top)
    for top in dirs:
        if top in ("skills", ".github"):
            continue
        for n in zf.namelist():
            if prefix and not n.startswith(prefix):
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
    DEST.mkdir(parents=True, exist_ok=True)
    env_lines = [l for l in ENV.read_text(encoding="utf-8").splitlines() if l.strip()] if ENV.exists() else []
    cur = ""
    for l in env_lines:
        if l.startswith("SKILLS="):
            cur = l.split("=", 1)[1].strip()
    cur_set = {x.strip() for x in cur.split(",") if x.strip()}

    for slug, (zip_name, root) in SOURCES.items():
        zf = _open_zip(zip_name)
        if zf is None:
            print(f"  [skip] {slug}: zip 缺失 {zip_name}", file=sys.stderr)
            continue
        md = extract_skill_md(zf, root)
        src_dir = DEST / slug
        if src_dir.exists():
            shutil.rmtree(src_dir)
        src_dir.mkdir(parents=True, exist_ok=True)
        # 只写小写 skill.md（中文可执行版）。注意：Windows 大小写不敏感，
        # 若再写同名 SKILL.md 会把中文覆盖掉，因此便携版另存 SKILL.portable.md。
        (src_dir / "skill.md").write_text("\n".join(ZH[slug]) + "\n", encoding="utf-8")
        if md:
            (src_dir / "SKILL.portable.md").write_text(md, encoding="utf-8")
        assets = copy_assets(zf, root, src_dir)
        zf.close()
        if slug not in cur_set:
            cur_set.add(slug)
        print(f"  [installed] {slug}  (assets: {','.join(assets) if assets else 'none'})")

    new_cur = ",".join(sorted(cur_set))
    env_out = [l for l in env_lines if not l.startswith("SKILLS=")]
    env_out.append("SKILLS=" + new_cur)
    ENV.write_text("\n".join(env_out) + "\n", encoding="utf-8")
    print("\nSKILLS =", new_cur)
    print("已安装到 skills/（含资产）。请重启 webapp。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
