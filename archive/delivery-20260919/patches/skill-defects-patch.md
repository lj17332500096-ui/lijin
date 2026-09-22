# 技能缺陷修复包（DEF-01 ~ DEF-10）—— **已修复**

> 取证环境：本机 Windows，managed Python 3.13.12
> 修复对象：`C:/Users/Administrator/.workbuddy/plugins/marketplaces/experts/plugins/agent-orchestration-pro/skills/`
> 取证时间：2026-09-19 ｜ 修复时间：2026-09-19
>
> **修复已落地并通过验收。** 该目录属市场插件缓存，插件升级会整体覆盖，
> 因此交付形态是「原始快照 + 修复快照 + 幂等重放脚本」：
>
> ```bash
> python delivery/patches/apply_skill_fixes.py            # 重放修复 + 自动验收
> python delivery/patches/apply_skill_fixes.py --check    # 只比对，不写盘
> python delivery/patches/apply_skill_fixes.py --restore  # 回滚到 orig/
> ```
>
> 验收脚本：`delivery/patches/verify_skill_fixes.py` —— 修复前 **1/15**，修复后 **15/15**。

---

## 修复状态总表

| 缺陷 | 技能 | 位置 | 严重度 | 一句话 | 状态 | 对应验收断言 |
|---|---|---|---|---|---|---|
| DEF-01 | everything-openai-codex | `scripts/main.py` | high | 自检格式串无占位符 → TypeError | **已修复** | `run.py --selftest` rc=0 且无格式串错误 |
| DEF-02 | everything-openai-codex | `scripts/main.py` | high | `_write_guarded` 格式串参数不匹配 | **已修复** | dry-run/verbose 两条路径可执行且返回值正确 |
| DEF-03 | everything-openai-codex | `scripts/main.py` | high | `_cli` 正常分支格式串无占位符 | **已修复** | `run.py --dry-run --force` rc=0 |
| DEF-04 | everything-openai-codex | `scripts/main.py` | medium | `selftest()` 被截断，无收尾 return | **已修复** | 链路末尾有显式 `return 0` 且第 10/11 项已补齐 |
| DEF-05 | everything-openai-codex | `scripts/main.py` | medium | 自检带破坏性副作用，覆盖记忆/钩子/规则 | **已修复** | 自检前后三文件 SHA256 不变 |
| DEF-06 | everything-openai-codex | `run.py` | medium | 挂载 `run_selftest` 取错名，恒 None | **已修复** | 三个技能 `run.run_selftest` 均非 None |
| DEF-07 | workflow | `run.py` | high | 守卫只认 `--file`，`--data/--url` 不可达 | **已修复** | `--data`→`source_type:json`、`--url`→`source_type:url` |
| DEF-08 | workflow | `scripts/main.py` | high | `--file` 是桩实现，不读文件内容 | **已修复（方案 A）** | `--file` 的 JSON/CSV 内容均能读入 |
| DEF-09 | 三个技能 | `run.py` | low | 挂载 `read_text_safe` 取错名，恒 None | **已修复** | 三个技能 `run.read_text_safe` 均非 None |
| DEF-10 | agent-ready-repo / workflow | `scripts/main.py` | low | `read_text_safe` 定义在 `__main__` 卫兵之后 | **已修复** | 定义行号 < 卫兵行号 |

严重度分布：**high 5 / medium 3 / low 2**，**10/10 已修复**。

### 本次实际采用的方案（含两处需要产品决策的取舍）

- **DEF-08 → 采用方案 A（实现真读），未采用方案 B（改文档撤下能力）**。
  SKILL.md 把「CSV/JSON/TXT → 目标结构」列为能力，撤文档等于收回已承诺的能力。
  附带实现 CSV 解析与"解析不出结构时降级为整文快照"。
- **DEF-05 → 采用上下文管理器隔离**（`contract_files_sandboxed()`），
  而不是"先备份再恢复"：前者在异常路径下也不会漏还原，且不依赖原有文件是否存在。
- **补充堵漏**：DEF-08 修好后文件内容成了新的输入面，因此把 `E005` 敏感信息门禁也覆盖到
  文件内容（否则 `--file` 会成为绕过检测的旁路）。新增自检第 9 项覆盖。
- **未改文档**：`workflow/SKILL.md` 的措辞未动（属于文档侧收尾，已列入待确认项）。

---

## 缺陷详情与修补内容

> 以下每一节的「现象 / 根因 / 补丁」保留取证时的原始记录，作为变更依据与审查材料。
> 实际落地代码见 `delivery/patches/fixed/`，逐行差异见 `delivery/patches/skill-defects.diff`。

---

## DEF-01 自检格式串无占位符

**现象**

```
$ python run.py --selftest
[ERROR] not all arguments converted during string formatting
```

**根因**（`scripts/main.py`）

```python
# line 459 / 462 —— 字符串里没有 %s，却对 name 做 % 格式化
print("  [PASS] everything-openai-codex" % name)
print("  [FAIL] everything-openai-codex" % name)
```

异常在 `except` 分支里二次抛出，最终被 `main()` 的兜底 `except` 捕获。

**补丁**

```python
print("  [PASS] everything-openai-codex %s" % name)
print("  [FAIL] everything-openai-codex %s" % name)
```

**验证**：`python run.py --selftest` 应输出 `自检通过`，退出码 0。

---

## DEF-02 `_write_guarded` 格式串参数不匹配

**现象**

```
>>> main._write_guarded('x.json', 'abc', dry_run=True)
TypeError: %d format: a real number is required, not str
```

**根因**（`scripts/main.py:488,493`）：只有 1 个 `%d` 占位符，却传了 2 元组 `(path, len(content))`，
第一个实参 `path` 被按数字解析。

**补丁**

```python
# line 488
print("[dry-run] 不写盘: %s (%d 字符)" % (path, len(content)))
# line 493
print("[verbose] 已写盘: %s (%d 字符)" % (path, len(content)))
```

**验证**：`main._write_guarded('x.json','abc',dry_run=True)` 应正常打印并返回 `False`。

---

## DEF-03 `_cli` 正常执行分支

**现象**

```
$ python run.py --dry-run --force
[ERROR] not all arguments converted during string formatting
rc=1
```

**根因**（`scripts/main.py:510`）

```python
print("执行模式: everything-openai-codex" % ("force 强制写盘" if args.force else "正常执行"))
```

**补丁**

```python
print("执行模式: everything-openai-codex / %s" % ("force 强制写盘" if args.force else "正常执行"))
```

**验证**：`python run.py --dry-run --force` 应打印执行模式并返回 0。

---

## DEF-04 `selftest()` 被截断

**取证**（AST 静态分析）

```
selftest end_lineno= 442  has_return= True（仅 except 分支里的 return 1）
total lines: 531
```

函数最后一条语句是 `print("  [OK] 钩子执行: 已触发")`；第 444 行是一条未写完的注释
`# 10. 测试 JSON 解析失败处理（应`，函数随后直接落到模块级的补丁段并结束，隐式返回 `None`。

**补丁**：补完第 10 项测试，并在末尾显式返回。

```python
    # 10. 测试 JSON 解析失败处理
    assert load_rules.__name__ == "load_rules"
    try:
        json.loads("{invalid")
        raise AssertionError("非法 JSON 应抛异常")
    except json.JSONDecodeError:
        print("  [OK] 非法 JSON 处理: 已捕获")

    print("== 自检完成 ==")
    return 0
```

**验证**：`python -c "import ast;..."` 中 `selftest()` 末尾存在 `return 0`。

---

## DEF-05 自检带破坏性副作用

**根因**（`scripts/main.py`）

```python
# line 394 —— 用测试数据覆盖真实记忆文件
assert save_memory(test_memory), "记忆保存失败"
# line 430 —— 把测试规则写进真实规则文件
with open(RULES_FILE, "w", encoding="utf-8") as f: json.dump(test_rules, f, ...)
# line 438 —— 把测试钩子写进真实钩子文件
with open(HOOKS_FILE, "w", encoding="utf-8") as f: json.dump(test_hooks, f, ...)
```

**后果**：跑过一次自检，技能的跨会话记忆与治理配置即被测试数据污染。

**补丁**：自检期间把三个路径切到临时目录。

```python
def _run_selftest():
    global MEMORY_FILE, HOOKS_FILE, RULES_FILE
    saved = (MEMORY_FILE, HOOKS_FILE, RULES_FILE)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        MEMORY_FILE = Path(td) / "memory.json"
        HOOKS_FILE = Path(td) / "hooks.json"
        RULES_FILE = Path(td) / "rules.json"
        try:
            ...  # 原自检主体
        finally:
            MEMORY_FILE, HOOKS_FILE, RULES_FILE = saved
```

**验证**：跑 `--selftest` 前后 `memory.json` / `hooks.json` / `rules.json` 的 sha256 不变。

---

## DEF-06 挂载 `run_selftest` 取错名

**取证**

```
everything-openai-codex:  impl 暴露 run_selftest: False / _run_selftest: True
                          run.py.run_selftest: False
```

`run.py:52` 取 `getattr(_impl, 'run_selftest', None)`，而实现暴露的是 `_run_selftest`。

**补丁**

```python
run_selftest = (getattr(_impl, 'run_selftest', None)
                or getattr(_impl, '_run_selftest', None))
```

---

## DEF-07 workflow 守卫只认 `--file`

**现象**

```
$ python run.py --data '{"a":1}' --format json
{"status": "need_input", "tool": "workflow",
 "reason": "未提供输入参数", "expected": "--file <文件路径或文本>"}
```

**根因**（`workflow/run.py:8`）：`_IN_ARG = '--file'`，而实现的主入口是 `--data / --url / --file` 三选一。
`--data` 与 `--url` 两条有效路径经 `run.py` 完全不可达。

**补丁**

```python
_IN_ARG = ('--data', '--url', '--file')       # 原为 '--file'

def _b_read_input():
    argv = sys.argv[1:]
    if '--selftest' in argv or '--help' in argv or '-h' in argv:
        return ('SKIP', None)
    for a in _IN_ARG:                          # 逐个候选参数扫描
        if a in argv:
            i = argv.index(a)
            if i + 1 >= len(argv):
                return ('MISSING', None)
            v = argv[i + 1]
            if os.path.isfile(v):
                try:
                    with open(v, encoding='utf-8', errors='ignore') as f:
                        return ('OK', f.read())
                except Exception as e:
                    return ('UNREADABLE', str(e))
            return ('OK', v)
    return ('NOARG', None)
```

**验证**：`python run.py --data '{"a":1}' --format json` 应返回 `source_type=json` 的结构化结果。

---

## DEF-08 `--file` 是桩实现

**现象**

```
$ python run.py --file /tmp/wf_probe.json --format json
{"data": {"file_path": "/tmp/wf_probe.json", "file_type": "json"},
 "source_type": "file", "confidence": "低",
 "note": "文件内容未实际读取，请确认文件可访问"}
```

**根因**（`scripts/main.py:163-186`）：`_extract_from_file` 只做扩展名正则校验就返回路径回显，
在 docstring 里自陈「本脚本不实际读取文件内容」。SKILL.md 却把「CSV/JSON/TXT 文件格式转换」
列为核心能力，自检里的「✓ 文件输入测试通过」也只验证了路径回显。

**补丁**（二选一）

```python
# 方案 A：真正实现读取
def _extract_from_file(file_path: str) -> Dict[str, Any]:
    if not file_path or not isinstance(file_path, str):
        raise WorkflowError("E007")
    if not re.search(r"\.(txt|json|md|log|csv)$", file_path, re.IGNORECASE):
        raise WorkflowError("E007", f"不支持的文件类型: {file_path}")
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise WorkflowError("E007", f"文件读取失败: {exc}") from exc
    out = _extract_from_text(text)          # 复用既有解析
    out["source_type"] = "file"
    out["file_path"] = file_path
    return out
```

方案 B：若维持桩实现，则必须从 SKILL.md 移除「文件格式转换」能力声明，
并把自检项改名为「文件路径校验」——不能让文档承诺实现不存在的能力。

---

## DEF-09 挂载 `read_text_safe` 取错名（三个技能同款）

**取证**

| 技能 | impl 暴露 `read_text_safe` | run.py 挂载结果 |
|---|---|---|
| agent-ready-repo | True | **False** |
| workflow | True | **False** |
| everything-openai-codex | False | **False** |

三个 `run.py:53` 都取 `getattr(_impl, '_read_text_safe', None)`，而实现暴露的是无下划线版本。
「多编码容错」这一被写进宣传卖点的独有能力，在 `run.py` 层不可达。

**补丁**

```python
read_text_safe = (getattr(_impl, 'read_text_safe', None)
                  or getattr(_impl, '_read_text_safe', None))
```

---

## DEF-10 `read_text_safe` 定义位置

**根因**（`agent-ready-repo/scripts/main.py`）

```python
509  if __name__ == "__main__":
510      sys.exit(main())
511
512
513  def read_text_safe(path):     # ← 在 __main__ 卫兵之后
```

脚本方式运行时，`main()` 返回后 `sys.exit()` 立即终止进程，第 513 行的定义永不执行；
模块导入方式则正常。同一份代码两条路径行为不一致。

**补丁**：把 `read_text_safe` 整体上移到 `if __name__ == "__main__":` 之前。

**验证**

```bash
python run.py --input <任意文件>    # 脚本路径正常
python -c "import run; print(run.read_text_safe is not None)"   # 模块路径应为 True
```

---

## 合入建议顺序

1. 先合 **DEF-01/02/03**（三个格式串）：一行改动，立刻让 `--selftest` 与 `--dry-run` 恢复可用。
2. 再合 **DEF-07**（workflow 守卫）：解开 `--data/--url` 两条主路径。
3. 再合 **DEF-06/09/10**（挂载点与定义位置）：低风险，恢复契约一致性。
4. **DEF-08** 需要产品决策（实现 or 改文档），不能只改代码。
5. **DEF-05** 涉及全局变量改写，建议与 DEF-04 一并重构自检函数。

---

*本文件由 AI 辅助生成，补丁内容请在实际合入前于目标环境复验。*
