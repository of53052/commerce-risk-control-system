"""接口层包。

分层职责（docs/ARCHITECTURE.md §9）：router 只做「取参 + 鉴权依赖 + 调 service +
组装响应」，**不含业务逻辑**。这样同一段业务能被脚本与测试直接调用，
而不必绕过 HTTP 层。
"""
