"""skill_compiler.py — 把 Claude 格式技能（带 YAML frontmatter 的 SKILL.md）编译为
「agent 通用技能 / 普通 agent 安装即用」的便携包。

输出到 agent-skills/<slug>/ 目录，每个技能 =：
  SKILL.md   — 便携标准指令（YAML frontmatter: name/description + 工具无关正文）
  skill.md   — 中文、自包含指令（本机 skills_loader 可直接用；由描述+正文生成脚手架，建议人工润色）
  skill.json — 清单（name/description/version/triggers/files/requires_tools）
  assets/…   — 技能附带的 模板/字体/引用 资源（原样拷贝，供支持资源的 agent 使用）

本脚本【不】改动 .env / skills/，不注册；只需把 agent-skills/<slug>/ 拷到目标工程的 skills/<slug>/，
并在 .env SKILLS 追加名字即可使用。

用法：
  python tools/skill_compiler.py --src H:\\skills-main.zip --skills academy-guide,brand-guidelines
  python tools/skill_compiler.py --src skills/academy-guide
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = ROOT / "agent-skills"
INNER = "skills-main"  # zip 顶层目录


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 `---\n yaml \n---` 前置块；返回 (meta, body)。
    支持折叠多行标量（`description: >` / `description: |` / 顶格 Key: 后接缩进块）。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    lines = m.group(1).splitlines()
    meta: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or (not line[:1].strip()):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        k, v = line.split(":", 1)
        key = k.strip().lower()
        val = v.strip().strip("'\"")
        # 折叠/块标量：`>`, `|`, `>-`, `|-`, `>+`, `|+`，或 Key: 后为空且后续行是缩进块
        is_block = val.strip("-+") in (">", "|") or val in (">", "|")
        if is_block or (val == "" and i + 1 < len(lines) and lines[i + 1][:1] in (" ", "\t")):
            cont, j = [], i + 1
            while j < len(lines) and (lines[j][:1] in (" ", "\t")):
                cont.append(lines[j].strip())
                j += 1
            if cont:
                val = (" ".join(cont) if val.strip("-+") == ">" else "\n".join(cont))
                i = j
        meta[key] = val
        i += 1
    return meta, m.group(2).strip()


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", name.lower()).strip("-")
    return s or "skill"


def _triggers(desc: str) -> list[str]:
    """从 description 粗提取触发短语（用于 manifest，非强依赖）。"""
    if not desc:
        return []
    out = re.findall(r'"([^"]{4,40})"', desc)
    return out[:8]


def _compiler_note(meta: dict) -> list[str]:
    return [
        "# 编译说明（自动生成）",
        f"- 来源技能：`{meta.get('name', '?')}`",
        "- 本 skill.md 由 SKILL.md 的 description + 正文结构**脚手架化**生成（未做语义级翻译润色）。",
        "- 若需更贴合本机「此刻/NOW」中文风格，请在发布前人工润色正文，或直接替换为人工中文稿。",
        "- 资源（templates/fonts/references）原样拷贝到 assets/；本机 skills_loader 只读 skill.md，",
        "  不自动托管这些资源，用到时需在 skill.md 里说明如何找到它们。",
    ]


def _build_chinese_skill(meta: dict, body: str) -> str:
    description = (meta.get("description") or "").strip()
    # 中文脚手架：目的 + 触发 + 执行要点（正文做"去英文产品词"的最小清理占位）
    title = meta.get("name") or "skill"
    purpose = description or f"处理与“{title}”相关的请求。"
    body_snippet = re.sub(r"\s+", " ", body)[:600]
    return (
        f"# {title}\n\n"
        f"## 用途\n{purpose}\n\n"
        f"## 触发\n当用户请求与“{title}”相关时，按下面的思路执行；"
        f"若请求与本技能无关，直接正常回答，不要生搬硬套。\n\n"
        f"## 要点\n（以下为自动提取的正文概览，需人工润色为可直接执行的中文步骤）\n\n"
        f"> {body_snippet}\n\n"
        + "\n".join(_compiler_note(meta))
    )


def _portable_skill_md(meta: dict, body: str) -> str:
    nl = "\n"
    yaml = (
        "---\n"
        f"name: {meta.get('name', 'skill')}\n"
        f"description: >\n  { (meta.get('description') or '').strip().replace(nl, '  ')[:400] }\n"
        "---\n\n"
    )
    return yaml + body


def compile_skill(skill_dir: Path, out_slug: str | None = None) -> Path:
    """把单个技能目录（含 SKILL.md）编译到 agent-skills/<slug>/。返回输出目录。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        skill_md = skill_dir / "skill.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"{skill_dir} 缺少 SKILL.md/skill.md")
    meta, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
    slug = out_slug or _slug(meta.get("name") or skill_dir.name)

    out = OUT_ROOT / slug
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "SKILL.md").write_text(_portable_skill_md(meta, body), encoding="utf-8")
    (out / "skill.md").write_text(_build_chinese_skill(meta, body), encoding="utf-8")

    # 拷贝附属资源（templates/fonts/references 等），放到 assets/ 下
    copied: list[str] = []
    for child in sorted(skill_dir.iterdir()):
        if child.is_dir() and child.name.lower() not in ("", "templates", "fonts", "references"):
            pass
        if child.is_dir():
            dest = out / "assets" / child.name
            shutil.copytree(child, dest, dirs_exist_ok=True)
            copied.append(child.name)

    manifest = {
        "name": meta.get("name") or slug,
        "slug": slug,
        "version": "1.0",
        "description": (meta.get("description") or "").strip()[:300],
        "triggers": _triggers(meta.get("description") or ""),
        "files": {
            "portable": "SKILL.md",
            "agent_zh": "skill.md",
            "assets": copied,
        },
        "requires_tools": bool((skill_dir / "tools.py").is_file()),
        "agent_hint": "复制本目录到目标工程的 skills/<slug>/，并在 .env 的 SKILLS= 追加 slug；"
                      "通用 agent 可直接读取 SKILL.md。",
    }
    (out / "skill.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def collect_skills(src: Path) -> dict[str, Path]:
    """从 zip 或目录收集技能目录：<slug> -> 目录。"""
    out: dict[str, Path] = {}
    tmp = ROOT / ".tmp_skill_extract"
    if src.suffix.lower() == ".zip":
        if tmp.exists():
            shutil.rmtree(tmp)
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        base = tmp / INNER if (tmp / INNER).is_dir() else tmp
        for d in sorted(base.glob("skills/*")):
            if d.is_dir():
                out[_slug(d.name)] = d
    else:
        # 单个目录或包含多个技能子目录
        if (src / "skills").is_dir():
            base = src / "skills"
            for d in sorted(base.glob("*")):
                if d.is_dir() and (d / "SKILL.md").is_file():
                    out[_slug(d.name)] = d
        else:
            out[_slug(src.name)] = src
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="zip 路径 或 技能目录/含 skills/ 的目录")
    ap.add_argument("--skills", default="", help="逗号分隔的 slug 子集；空=全部")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"src 不存在: {src}", file=sys.stderr)
        return 1
    skills = collect_skills(src)
    want = {s.strip() for s in args.skills.split(",") if s.strip()}
    ok = []
    for slug, d in skills.items():
        if want and slug not in want:
            continue
        try:
            o = compile_skill(d, out_slug=slug)
            ok.append(str(o))
        except Exception as e:
            print(f"  [skip] {slug}: {e}", file=sys.stderr)
    print(f"compiled {len(ok)} skill(s) -> {OUT_ROOT}:")
    for o in ok:
        print("  ", o)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
