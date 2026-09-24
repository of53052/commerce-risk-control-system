"""名单类特征键的中心定义（无依赖的叶子模块）。

**为什么单独一个文件**：名单标记（subject_blacklist / device_gray_flag 等）在语义上是
特征，但由名单服务产出。若把键名定义在 ``list_service``，则
``feature_engine`` 需要 import 它，而 ``list_service`` 又需要 ``feature_engine``
的 ``FeatureSpec`` 来声明自己的特征 —— 形成循环导入。

解决办法是最常见的依赖倒置：把**双方都要用的常量**下沉到一个谁都不依赖的模块。
于是依赖方向变成::

    flags  ──> feature_engine ──> list_service

三者都不需要互相反向引用，注册表回填由 ``app/models/__init__.py`` 在导入期完成。
"""

from __future__ import annotations

# 主体黑/白名单标记（决定动作，见 docs/PRD.md §8.1）
BLACKLIST_FLAG_KEY = "subject_blacklist"
WHITELIST_FLAG_KEY = "subject_whitelist"

# 灰名单标记 -> 键名。灰名单不决定动作，只作为加成特征参与规则与模型打分。
GRAY_FLAG_KEYS: dict[str, str] = {
    "user": "subject_gray_flag",
    "phone": "phone_gray_flag",
    "ip": "ip_gray_flag",
    "device": "device_gray_flag",
    "address": "address_gray_flag",
}

# 名单服务会产出的全部特征键。
# 用途：
#   1. 规则字段白名单 —— 规则可以引用名单标记（如 subject_gray_flag == 1），
#      这些键不在特征注册表里（它们不是窗口聚合，而是名单匹配结果）；
#   2. 默认值填充 —— 未命中时给 0/False，让规则总能拿到确定值，
#      而不是「键不存在 → 条件判 false + missing_fields 告警」。
FLAG_FEATURE_KEYS: tuple[str, ...] = (
    BLACKLIST_FLAG_KEY,
    WHITELIST_FLAG_KEY,
    *GRAY_FLAG_KEYS.values(),
)
