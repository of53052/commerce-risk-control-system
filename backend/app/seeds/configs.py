"""sys_config 种子：把配置注册表（app/services/config_service.py）落成数据行。

为什么种子要"从注册表生成"而不是手写一份 SQL：
    手写必然与注册表漂移。这里以 ``CONFIG_SPECS`` 为唯一来源，
    新增一条配置只需改注册表，种子自动跟随。
"""

from __future__ import annotations

import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sys import SysConfig
from app.services.config_service import CONFIG_SPECS, invalidate_cache

NAME = "configs"


def _serialize(value, value_type: str) -> str:
    """统一序列化为字符串存库（sys_config.config_value 是 Text）。"""
    if value_type == "json":
        return orjson.dumps(value).decode("utf-8")
    if value_type == "bool":
        return "true" if value else "false"
    return str(value)


def run(db: Session) -> int:
    existing = {row.config_key: row for row in db.execute(select(SysConfig)).scalars()}
    affected = 0

    for spec in CONFIG_SPECS:
        raw = _serialize(spec.default, spec.value_type)
        row = existing.get(spec.key)
        if row is None:
            db.add(
                SysConfig(
                    config_key=spec.key,
                    config_value=raw,
                    value_type=spec.value_type,
                    description=spec.description,
                    updated_by="seed",
                )
            )
            affected += 1
        elif row.config_value != raw or row.value_type != spec.value_type:
            # 只在"与注册表不一致"时更新，避免每次 init_db 都刷 updated_at
            row.config_value = raw
            row.value_type = spec.value_type
            row.description = spec.description
            row.updated_by = "seed"
            affected += 1

    invalidate_cache()
    return affected

