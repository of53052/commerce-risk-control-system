"""电商风险控制系统 —— 后端应用包。

分层约定（详见 docs/ARCHITECTURE.md §4）：
    api -> services -> (expression | models) -> core
禁止反向依赖：services 不得导入 api，models 不得导入 services。
"""

__version__ = "0.1.0"
