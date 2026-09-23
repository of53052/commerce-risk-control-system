"""运行期可配参数访问层（sys_config）。

三个设计要点：

1. **注册表即契约**：``CONFIG_SPECS`` 是这些参数的唯一权威定义（键名、类型、默认值、说明）。
   写入（种子 / 配置页）与读取（决策热路径）都以它为准，避免出现
   「一处写 '60'、另一处按 '60.0' 读」这类只有线上才发现的错位。
2. **代码兜底**：``sys_config`` 缺键时返回注册表默认值，因此「清空配置表」不会让系统不可用
   —— 这是 app/models/sys.py 里 SysConfig 注释已经做出的承诺，在此落地。
3. **进程内缓存 + 短 TTL**：阈值与 α 在每次决策里都要读，一事件一次 SELECT 是纯浪费；
   缓存 10 秒，并由写接口主动 ``invalidate_cache()`` 保证「保存后立即生效」的体验。
   为什么不用 Redis：这是单实例部署（docs/ARCHITECTURE.md §1 约束），
   进程内缓存少一次网络往返，且不存在多实例不一致问题。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sys import SysConfig


@dataclass(frozen=True)
class ConfigSpec:
    """一条配置的元定义。"""

    key: str
    value_type: str  # str / int / float / bool / json
    default: Any
    description: str


# ---- 配置注册表（docs/PRD.md §13：阈值、α、合案窗口、窗口档位、融合模式、冲突策略、严格模式）----
CONFIG_SPECS: tuple[ConfigSpec, ...] = (
    ConfigSpec(
        key="risk_threshold_review",
        value_type="int",
        default=60,
        description="综合分达到该值判为中风险（mid），动作为 Review 并生成人工审核案件",
    ),
    ConfigSpec(
        key="risk_threshold_reject",
        value_type="int",
        default=80,
        description="综合分达到该值判为高风险（high），动作为 Reject 并自动建案",
    ),
    ConfigSpec(
        key="fusion_alpha",
        value_type="float",
        default=0.3,
        description="融合权重 α：risk_score = rule_score + α × model_score",
    ),
    ConfigSpec(
        key="fusion_mode",
        value_type="str",
        default="additive",
        description="融合模式：additive（默认）/ max / weighted",
    ),
    ConfigSpec(
        key="list_conflict_policy",
        value_type="str",
        default="black_first",
        description="名单黑白同时命中时的优先级策略：black_first / whitelist_first / priority",
    ),
    ConfigSpec(
        key="rule_strict_mode",
        value_type="bool",
        default=False,
        description="规则求值严格模式：false=字段缺失判 false 并告警；true=求值失败并记错误",
    ),
    ConfigSpec(
        key="case_merge_window_minutes",
        value_type="int",
        default=30,
        description="案件合案窗口（分钟）：同主体同场景在窗口内重复触发则合并到既有案件",
    ),
    ConfigSpec(
        key="feature_windows",
        value_type="json",
        default={"1h": 3600, "24h": 86400, "7d": 604800},
        description="特征滑动窗口档位：档位名 -> 秒数，特征注册表按此展开",
    ),
)

CONFIG_DEFAULTS: dict[str, Any] = {spec.key: spec.default for spec in CONFIG_SPECS}
CONFIG_SPECS_BY_KEY: dict[str, ConfigSpec] = {spec.key: spec for spec in CONFIG_SPECS}

# 缓存有效期（秒）。取值权衡：太长则策略师改阈值后长时间不生效，太短则失去缓存意义。
_CACHE_TTL_SECONDS = 10
_cache: dict[str, Any] | None = None
_cache_at: float = 0.0


def invalidate_cache() -> None:
    """清空缓存。配置写接口在提交后调用，保证「保存即生效」。"""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def _coerce(raw: str, value_type: str) -> Any:
    """按注册表声明的类型转换字符串值。

    转换失败不抛异常而是返回 None，由调用方回退默认值：
    一条坏配置不应该让整条决策链路 500。
    """
    try:
        if value_type == "int":
            return int(raw)
        if value_type == "float":
            return float(raw)
        if value_type == "bool":
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        if value_type == "json":
            return orjson.loads(raw)
        return raw
    except (ValueError, TypeError, orjson.JSONDecodeError):
        return None


def get_config_map(db: Session, use_cache: bool = True) -> dict[str, Any]:
    """返回「注册表默认值 ← 数据库覆盖」合并后的完整配置字典。"""
    global _cache, _cache_at

    now = time.monotonic()
    if use_cache and _cache is not None and (now - _cache_at) < _CACHE_TTL_SECONDS:
        return _cache

    merged: dict[str, Any] = dict(CONFIG_DEFAULTS)
    rows = db.execute(select(SysConfig)).scalars().all()
    for row in rows:
        spec = CONFIG_SPECS_BY_KEY.get(row.config_key)
        # 只认注册表里的键：表里出现陌生键说明代码已回滚但库还留着新配置，
        # 静默忽略比"把未知键塞进配置字典"安全得多。
        if spec is None:
            continue
        value = _coerce(row.config_value, spec.value_type)
        if value is not None:
            merged[spec.key] = value

    if use_cache:
        _cache = merged
        _cache_at = now
    return merged


def get_value(db: Session, key: str, use_cache: bool = True) -> Any:
    """读取单个配置值；键不在注册表中直接报错（拼错键名要立刻暴露，不能静默用默认值）。"""
    if key not in CONFIG_SPECS_BY_KEY:
        raise KeyError(f"未在 CONFIG_SPECS 注册的配置键：{key}")
    return get_config_map(db, use_cache=use_cache)[key]


def get_int(db: Session, key: str) -> int:
    return int(get_value(db, key))


def get_float(db: Session, key: str) -> float:
    return float(get_value(db, key))


def get_bool(db: Session, key: str) -> bool:
    return bool(get_value(db, key))


def get_str(db: Session, key: str) -> str:
    return str(get_value(db, key))

