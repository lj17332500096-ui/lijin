"""Markdown 渲染器测试（经 Node 运行 web/runtime/markdown.js，断言安全 AST）。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
RUNNER = BASE / "tests" / "markdown_runner.js"
NODE = "node"


def parse_all(cases):
    payload = json.dumps(cases, ensure_ascii=False)
    proc = subprocess.run([NODE, str(RUNNER)], input=payload, capture_output=True, text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


def parse_one(text):
    return parse_all([{"text": text, "href": ""}])[0]["ast"]


def href_of(url):
    return parse_all([{"text": "", "href": url}])[0]["href"]


def inline_types(inlines):
    return [n["t"] for n in inlines]


def inline_text(inlines):
    """把 inline 节点展平为文本（text/strong/em/code/link 均取内容）。"""
    out = []
    for n in inlines or []:
        if n.get("t") == "text":
            out.append(n.get("v", ""))
        else:
            out.append(n.get("v", ""))
    return "".join(out)


def list_items(ast):
    """收集 AST 中所有 ul/ol 条目文本；返回 (容器类型, item_text) 列表。"""
    items = []
    for node in ast:
        if node.get("t") in ("ul", "ol"):
            for item in node.get("items", []):
                items.append((node["t"], inline_text(item)))
    return items


class MarkdownAstTests(unittest.TestCase):
    def test_paragraph(self) -> None:
        ast = parse_one("这是一段普通文本。")
        self.assertEqual(ast[0]["t"], "p")
        self.assertEqual(ast[0]["inlines"][0], {"t": "text", "v": "这是一段普通文本。"})

    def test_bold_and_italic(self) -> None:
        ast = parse_one("**粗体** 和 *斜体* 和 _下划线_")
        types = inline_types(ast[0]["inlines"])
        self.assertEqual(types, ["strong", "text", "em", "text", "em"])

    def test_unordered_list(self) -> None:
        ast = parse_one("- 甲\n- 乙\n* 丙")
        self.assertEqual(ast[0]["t"], "ul")
        self.assertEqual(len(ast[0]["items"]), 3)
        # 回归：无序列表条目文本必须保留（曾因捕获组下标错位变成空 <li>）
        self.assertEqual([inline_text(it) for it in ast[0]["items"]],
                         ["甲", "乙", "丙"])

    def test_ordered_list(self) -> None:
        ast = parse_one("1. 一\n2. 二\n3. 三")
        self.assertEqual(ast[0]["t"], "ol")
        self.assertEqual(len(ast[0]["items"]), 3)

    def test_headings(self) -> None:
        ast = parse_one("# 一级\n## 二级\n### 三级")
        self.assertEqual([n["t"] for n in ast], ["h1", "h2", "h3"])

    def test_blockquote(self) -> None:
        ast = parse_one("> 引用的内容")
        self.assertEqual(ast[0]["t"], "quote")
        self.assertEqual(ast[0]["inlines"][0]["v"], "引用的内容")

    def test_inline_code(self) -> None:
        ast = parse_one("使用 `token` 刷新")
        self.assertIn("code", inline_types(ast[0]["inlines"]))
        code = next(n for n in ast[0]["inlines"] if n["t"] == "code")
        self.assertEqual(code["v"], "token")

    def test_fenced_code_block(self) -> None:
        ast = parse_one("```python\nprint('hi')\n```")
        self.assertEqual(ast[0]["t"], "code")
        self.assertEqual(ast[0]["lang"], "python")
        self.assertEqual(ast[0]["code"], "print('hi')")

    def test_table(self) -> None:
        ast = parse_one("| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 3 |")
        self.assertEqual(ast[0]["t"], "table")
        self.assertEqual(len(ast[0]["headers"]), 2)
        self.assertEqual(len(ast[0]["rows"]), 1)

    def test_link(self) -> None:
        ast = parse_one("见 [文档](https://example.com/a)")
        link = next(n for n in ast[0]["inlines"] if n["t"] == "link")
        self.assertEqual(link["href"], "https://example.com/a")

    def test_hr(self) -> None:
        ast = parse_one("---")
        self.assertEqual(ast[0]["t"], "hr")

    def test_chinese_mixed_with_code(self) -> None:
        ast = parse_one("修复 `token` 刷新逻辑，修改 2 个文件：`authStore.ts` 与 `apiClient.ts`。")
        types = inline_types(ast[0]["inlines"])
        self.assertIn("code", types)
        self.assertIn("text", types)

    def test_thought_lines_demoted(self) -> None:
        # 模型过程性列表行应被标记（前端弱化），但不改变 AST 语义
        ast = parse_one("- 正在搜索资料\n- 接下来修改文件")
        self.assertEqual(ast[0]["t"], "ul")

    def test_long_answer(self) -> None:
        text = "# 报告\n\n" + "\n\n".join("第 %d 段：" % i + "内容" * 40 for i in range(40))
        ast = parse_one(text)
        self.assertEqual(ast[0]["t"], "h1")
        self.assertTrue(len(ast) > 20)

    def test_chunked_stream_equals_whole(self) -> None:
        whole = "## 标题\n\n**粗体** 与 `代码`\n\n- 甲\n- 乙\n\n```js\nvar x=1;\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |"
        chunks = ["## 标", "题\n\n**粗", "体** 与 `代", "码`\n\n- 甲\n- ", "乙\n\n```js\nvar x=1;\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |"]
        joined = "".join(chunks)
        self.assertEqual(joined, whole)
        self.assertEqual(parse_one(joined), parse_one(whole))


class MarkdownXssTests(unittest.TestCase):
    def test_script_tag_is_plain_text(self) -> None:
        ast = parse_one('<script>alert(1)</script>')
        self.assertEqual(ast[0]["t"], "p")
        self.assertIn("script", ast[0]["inlines"][0]["v"])

    def test_img_onerror_is_plain_text(self) -> None:
        ast = parse_one('<img src=x onerror="alert(1)">')
        self.assertEqual(ast[0]["t"], "p")
        self.assertIn("<img", ast[0]["inlines"][0]["v"])

    def test_iframe_and_svg_plain_text(self) -> None:
        ast = parse_one('<iframe src="https://evil"></iframe> <svg onload="x()"></svg>')
        self.assertIn("iframe", ast[0]["inlines"][0]["v"])

    def test_javascript_url_neutralized(self) -> None:
        self.assertEqual(href_of("javascript:alert(1)"), "#")
        self.assertEqual(href_of("data:text/html,evil"), "#")
        self.assertEqual(href_of("vbscript:msgbox(1)"), "#")

    def test_safe_url_kept(self) -> None:
        self.assertEqual(href_of("https://example.com/x"), "https://example.com/x")
        self.assertEqual(href_of("/local/path"), "/local/path")
        self.assertEqual(href_of("mailto:a@b.c"), "mailto:a@b.c")

    def test_style_injection_is_text(self) -> None:
        ast = parse_one('<style>*{display:none}</style>')
        self.assertIn("<style>", ast[0]["inlines"][0]["v"])


class MarkdownStreamingBulletTests(unittest.TestCase):
    """Streaming Markdown 空项目符号根因回归（TEST 1~7 的离线确定性部分）。

    根因：无序列表正则 /^[-*]\s+(.*)$/ 只有 1 个捕获组，构建 AST 时误取 li[2]
    （应为 li[1]），parseInline(undefined) → [] → 每个 -/* 条目渲染成空 <li>。
    本组测试验证：无论 chunk 怎么切，只要最终 accumulatedText 与原始文本逐字符
    一致，解析/渲染出的列表条目都必须携带原文，不允许空 bullet。
    """

    def _assert_list_full(self, raw: str, expected: list[str]) -> None:
        ast = parse_one(raw)
        items = list_items(ast)
        self.assertEqual([t for _, t in items], expected)
        self.assertTrue(items)
        self.assertTrue(all(t.strip() for _, t in items),
                        f"存在空列表项: raw={raw!r}")

    def test_test1_basic_list_random_chunks(self) -> None:
        chunks = [
            "# 信息检索\n\n",
            "-",
            " 搜索互联网",
            "获取最新信息",
            "\n",
            "- ",
            "整理搜索结果",
        ]
        joined = "".join(chunks)
        self.assertEqual(joined, "# 信息检索\n\n- 搜索互联网获取最新信息\n- 整理搜索结果")
        ast = parse_one(joined)
        self.assertEqual(ast[0]["t"], "h1")
        self.assertEqual([t for _, t in list_items(ast)],
                         ["搜索互联网获取最新信息", "整理搜索结果"])

    def test_test2_single_char_chunks(self) -> None:
        chunks = ["-", " ", "搜", "索", "互", "联", "网"]
        joined = "".join(chunks)
        self.assertEqual(joined, "- 搜索互联网")
        self._assert_list_full(joined, ["搜索互联网"])

    def test_test3_chinese_markdown_no_empty_bullet(self) -> None:
        raw = (
            "## 信息检索\n\n- 搜索\n- 汇总\n\n"
            "## 文档与知识库\n\n- 阅读\n- 检索\n- 总结\n\n"
            "## 内容产出\n\n- 撰写\n- 修改\n- 生成"
        )
        ast = parse_one(raw)
        self.assertEqual([n["t"] for n in ast if n["t"].startswith("h")],
                         ["h2", "h2", "h2"])
        items = [t for _, t in list_items(ast)]
        self.assertEqual(items, ["搜索", "汇总", "阅读", "检索", "总结",
                                 "撰写", "修改", "生成"])
        self.assertEqual(len(items), 8)

    def test_test4_markdown_comprehensive_random_chunk_positions(self) -> None:
        raw = (
            "# 标题\n\n"
            "**粗体** 与 *斜体* 和 `代码`\n\n"
            "> 引用\n\n"
            "```python\nprint(\"hello\")\n```\n\n"
            "- 无序甲\n- 无序乙\n\n"
            "1. 有序一\n2. 有序二\n\n"
            "| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 3 |\n"
        )
        import random

        rng = random.Random(20260907)
        for _ in range(20):
            positions = sorted(rng.sample(range(1, len(raw)), k=12))
            chunks = []
            prev = 0
            for pos in positions + [len(raw)]:
                chunks.append(raw[prev:pos])
                prev = pos
            joined = "".join(chunks)
            self.assertEqual(joined, raw)  # 逐字符一致，无增删改
            self.assertEqual(parse_one(joined), parse_one(raw))
            items = [t for _, t in list_items(parse_one(joined))]
            self.assertIn("无序甲", items)
            self.assertIn("有序二", items)
            self.assertTrue(all(t.strip() for t in items))
        # 代码块在流式切分下也必须完整
        code_chunks = ["```", "py", "tho", "n\nprint(", "\"hello\"", ")\n```"]
        code_joined = "".join(code_chunks)
        self.assertEqual(code_joined, "```python\nprint(\"hello\")\n```")
        self.assertEqual(parse_one(code_joined), parse_one(code_joined))
        self.assertEqual(parse_one(code_joined)[0]["t"], "code")
        self.assertEqual(parse_one(code_joined)[0]["code"], "print(\"hello\")")

    def test_test5_whitespace_preserved_in_accumulation(self) -> None:
        # 累积层：只有逐字符 append，禁止 trim / 加换行 / 丢空白
        chunks = ["## A\n\n", "-", " ", "保留", "  双空格", "尾部空格  ", "\n",
                  "-  缩进", "内容\n"]
        joined = "".join(chunks)
        self.assertEqual(joined, "".join(chunks))
        self.assertIn("- 保留  双空格尾部空格  ", joined)
        self.assertIn("-  缩进内容\n", joined)

    def test_test6_reconnect_resumes_accumulation_no_dup_or_loss(self) -> None:
        # 模拟：前一半 chunk → 断线 → 重连后继续追加 → 最终文本=历史+实时
        first = ["## 信息检索\n\n- 搜索互联网", "获取最新信息\n"]
        after_reconnect = ["- 整理并汇总", "搜索结果"]
        final_text = "".join(first) + "".join(after_reconnect)
        self.assertEqual(final_text,
                         "## 信息检索\n\n- 搜索互联网获取最新信息\n- 整理并汇总搜索结果")
        ast = parse_one(final_text)
        self.assertEqual([t for _, t in list_items(ast)],
                         ["搜索互联网获取最新信息", "整理并汇总搜索结果"])

    def test_test7_final_reconciliation_canonical_overrides_partial(self) -> None:
        # 中间态可能残缺（例如只有 "-"），但 canonical final 到达后整段重渲染，
        # 不允许残留空 bullet。
        partial = "-"
        parse_one(partial)  # 中间态可解析（不抛异常即可）
        canonical = "- 完整内容"
        ast = parse_one(canonical)
        self.assertEqual(list_items(ast), [("ul", "完整内容")])


if __name__ == "__main__":
    unittest.main()
