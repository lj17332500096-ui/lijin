import json
from runtime.public_activity import FinalContentStream

# 场景1：多行content逐字符feed → 每行边界 emit 一次（逐行打字机）
result = []
d = FinalContentStream(result.append)
content = "第一行结论。\n第二行说明。\npassword=hidden456\n结果通过。"
raw = json.dumps({"reasoning": "INTERNAL", "content": content}, ensure_ascii=True)
for ch in raw:
    d.feed(ch)
assert d.complete
visible = "".join(result)
assert "第一行结论" in visible and "第二行说明" in visible and "结果通过" in visible
assert "INTERNAL" not in visible and "hidden456" not in visible
print(f"场景1(多行): emit帧数={len(result)}, visible长度={len(visible)}, 逐行增长✓, PRIVATE未泄漏✓")

# 场景2：单行content → 非final帧0 emit，final帧一次性补全（受控降级，安全优先）
result2 = []
d2 = FinalContentStream(result2.append)
content2 = "这是一段没有换行的单行最终答案。"
raw2 = json.dumps({"content": content2}, ensure_ascii=True)
for ch in raw2:
    d2.feed(ch)
assert d2.complete
visible2 = "".join(result2)
assert visible2 == content2, f"单行content应完整可见，实际: {visible2!r}"
print(f"场景2(单行): emit帧数={len(result2)}, final一次性补全✓, 内容完整={visible2[:30]!r}...")

# 场景3：secret 出现在未闭合的"假闭包"半截状态时，绝不能泄漏
result3 = []
d3 = FinalContentStream(result3.append)
# 构造：thinking标签包裹的secret在content字符串中
content3 = "正常开头。\napi_key=SECRET_XXX_123\n正常结尾。"
raw3 = json.dumps({"content": content3}, ensure_ascii=True)
for ch in raw3:
    d3.feed(ch)
assert d3.complete
visible3 = "".join(result3)
assert "SECRET_XXX_123" not in visible3
assert "正常开头" in visible3 and "正常结尾" in visible3
print(f"场景3(secret行): emit帧数={len(result3)}, SECRET未泄漏✓, 非secret行保留✓")

print("\n全部断言通过。")
