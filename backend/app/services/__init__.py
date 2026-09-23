"""服务层包。

依赖方向（docs/ARCHITECTURE.md §4）：api → services → models / db / expression。
服务层不导入任何 FastAPI 类型，保证它可以被脚本、测试直接调用。
"""

