
# 直接验证修复后的 calc.py 逻辑（等价于运行测试）
from calc import add, multiply

# 测试 add
result_add = add(1, 2)
print(f"add(1, 2) = {result_add}, 期望 3, {'通过' if result_add == 3 else '失败'}")

# 测试 multiply
result_mul = multiply(2, 3)
print(f"multiply(2, 3) = {result_mul}, 期望 6, {'通过' if result_mul == 6 else '失败'}")

print("验证完成")
