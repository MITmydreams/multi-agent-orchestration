#!/usr/bin/env python3
"""测试脚本 - 验证正确的模块导入方式"""
import sys
print(f"Python版本: {sys.version}")
print(f"当前工作目录: {sys.path[0]}")
print(f"Python路径:")
for p in sys.path[:5]:
    print(f"  - {p}")

# 测试导入
try:
    import src.models.base
    print("\n✅ 成功导入 src.models.base")
except ImportError as e:
    print(f"\n❌ 导入失败: {e}")
