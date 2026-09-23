"""结构化日志。

要点：
1. 采用 JSON 行格式，字段固定（ts/level/module/trace_id/decision_id/event_id/cost_ms/msg），
   便于用 rg / jq 直接过滤「某次决策的全链路」（docs/ARCHITECTURE.md §11）。
2. trace_id / decision_id 等使用 contextvars 传递：在请求入口设置一次，
   下游任意模块打日志时自动带上，无需层层传参。
3. 敏感字段脱敏：手机号保留前 3 后 2 位，token / api_key 只留前 6 位。
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

from app.core.config import settings

# 请求级上下文；默认 "-" 便于日志解析时不做空值判断。
trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="-")
decision_id_ctx: ContextVar[str] = ContextVar("decision_id", default="-")
event_id_ctx: ContextVar[str] = ContextVar("event_id", default="-")

_SENSITIVE_KEYS = {"password", "api_key", "x-api-key", "token", "authorization"}


def mask_phone(phone: str | None) -> str | None:
    """手机号脱敏：13812345678 -> 138****5678。非 11 位则原样返回长度掩码。"""
    if not phone:
        return phone
    if len(phone) < 7:
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


def mask_secret(value: str | None) -> str | None:
    """密钥类字段脱敏：只保留前 6 位。"""
    if not value:
        return value
    return value[:6] + "***" if len(value) > 6 else "***"


def _sanitize(extra: dict) -> dict:
    """对 extra 字段做递归脱敏，避免明文密钥/手机号落盘。"""
    clean: dict = {}
    for key, value in extra.items():
        lower = key.lower()
        if lower in _SENSITIVE_KEYS:
            clean[key] = mask_secret(str(value))
        elif lower in {"phone", "receiver_phone"}:
            clean[key] = mask_phone(str(value))
        elif isinstance(value, dict):
            clean[key] = _sanitize(value)
        else:
            clean[key] = value
    return clean


class JsonFormatter(logging.Formatter):
    """输出单行 JSON，便于日志采集与检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "module": record.name,
            "trace_id": trace_id_ctx.get(),
            "decision_id": decision_id_ctx.get(),
            "event_id": event_id_ctx.get(),
            "msg": record.getMessage(),
        }
        if isinstance(getattr(record, "extra_fields", None), dict):
            payload.update(_sanitize(record.extra_fields))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """初始化根日志。重复调用安全（幂等）。"""
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    # 清掉 uvicorn 默认 handler，避免同一行日志输出两次
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def log_kv(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """带结构化字段的日志入口：log_kv(logger, logging.INFO, "决策完成", cost_ms=12)。"""
    logger.log(level, msg, extra={"extra_fields": fields})
