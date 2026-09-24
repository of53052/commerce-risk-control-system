"""模型引擎：加载 model.json、线性推理、贡献度 top-5。

**为什么模型是文件而不是服务**（docs/ARCHITECTURE.md 的 ADR）：
    单机演示 + 单人维护的规模下，为一个逻辑回归起一个推理服务，
    带来的部署、版本、超时、熔断成本远大于收益。模型是几百个浮点数，
    进程内加载即可，切换版本靠"原子替换内存引用"。

模型文件契约（字段固定，加载时强校验）::

    {
      "version": "v1",
      "feature_names": [...],     # 推理时严格按此顺序取数
      "log1p": true,              # 训练前是否做了 log1p
      "mean": [...], "std": [...],
      "weights": [...], "intercept": -2.31,
      "metrics": {...}, "label_definition": "..."
    }

**缺失特征取标准化后的 0**（即训练均值所在的位置，该维度不贡献分数），
并记入 ``imputed_fields``；绝不悄悄按 0 处理 —— 0 在 log1p 与 z-score 之后
是一个有明确含义的值（"该特征处于样本最小值附近"），
会让模型给出一个看似正常但完全错误的分。

``mean`` / ``std`` 存的是**变换后**（log1p 之后）空间的统计量，
因此缺失分支必须直接跳过变换（详见 ``predict`` 里的注释）。
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.decision import RcModelVersion

logger = logging.getLogger("app.services.model_engine")

# 模型产物目录：backend/models_artifacts/
ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "models_artifacts"


class ModelError(RuntimeError):
    """模型文件缺失或格式非法。"""


@dataclass
class ModelArtifact:
    """加载后的模型（不可变意图：切换版本时整体替换引用）。"""

    version: str
    feature_names: list[str]
    weights: list[float]
    intercept: float
    mean: list[float]
    std: list[float]
    log1p: bool
    metrics: dict[str, Any] = field(default_factory=dict)
    trained_at: str | None = None
    file_path: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, file_path: str | None = None) -> ModelArtifact:
        """从 JSON 字典构造，并做完整的一致性校验。

        校验的重点是**长度一致**：权重、均值、方差、特征名的长度必须相同。
        这类错误在训练脚本改动后极易出现，而它的表现是"分数看着正常但全是错的"，
        属于最难排查的一类问题，必须在加载时拦住。
        """
        required = ("version", "feature_names", "weights", "intercept", "mean", "std")
        missing = [key for key in required if key not in payload]
        if missing:
            raise ModelError(f"模型文件缺少字段：{missing}")

        feature_names = list(payload["feature_names"])
        weights = [float(value) for value in payload["weights"]]
        mean = [float(value) for value in payload["mean"]]
        std = [float(value) for value in payload["std"]]
        lengths = {len(feature_names), len(weights), len(mean), len(std)}
        if len(lengths) != 1:
            raise ModelError(
                f"模型文件维度不一致：feature_names={len(feature_names)}, "
                f"weights={len(weights)}, mean={len(mean)}, std={len(std)}"
            )

        return cls(
            version=str(payload["version"]),
            feature_names=feature_names,
            weights=weights,
            intercept=float(payload["intercept"]),
            mean=mean,
            std=std,
            log1p=bool(payload.get("log1p", False)),
            metrics=dict(payload.get("metrics") or {}),
            trained_at=payload.get("trained_at"),
            file_path=file_path,
        )


@dataclass
class ModelPrediction:
    """一次推理结果。"""

    enabled: bool
    version: str | None = None
    model_score: int = 0
    probability: float = 0.0
    contributions: list[dict[str, Any]] = field(default_factory=list)
    imputed_fields: list[str] = field(default_factory=list)
    cost_ms: int = 0
    reason: str | None = None


# 当前生效模型：模块级引用，切换时整体替换（读路径无锁）。
_active: ModelArtifact | None = None
_load_failed: str | None = None


def get_active() -> ModelArtifact | None:
    return _active


def load_from_file(path: Path | str) -> ModelArtifact:
    """从文件加载模型（不改变当前生效引用）。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ModelError(f"模型文件不存在：{file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelError(f"模型文件不是合法 JSON：{exc}") from exc
    return ModelArtifact.from_dict(payload, file_path=str(file_path))


def activate(artifact: ModelArtifact) -> None:
    """把模型设为当前生效版本（原子替换引用）。"""
    global _active, _load_failed
    _active = artifact
    _load_failed = None
    logger.info("模型已生效：version=%s features=%d", artifact.version, len(artifact.feature_names))


def load_active(db: Session | None = None) -> tuple[ModelArtifact | None, str | None]:
    """加载当前启用模型，返回 (模型, 失败原因)。

    找不到启用版本时**不抛错**：P0 早期（尚未训练模型）以及"模型文件被误删"时，
    决策链必须还能跑 —— 此时 ``model_score`` 记 0 并标注 ``model_disabled``，
    由规则分单独决策，而不是让整个事件接入 500。
    """
    global _load_failed
    if db is None:
        return _active, _load_failed

    row = db.execute(
        select(RcModelVersion).where(RcModelVersion.active.is_(True)).order_by(RcModelVersion.id.desc())
    ).scalars().first()
    if row is None:
        _load_failed = "no_active_model_version"
        return None, _load_failed

    path = Path(row.file_path)
    if not path.is_absolute():
        # rc_model_version.file_path 存的是相对 backend/ 的路径，
        # 便于仓库迁移后仍然有效
        path = Path(__file__).resolve().parents[2] / path
    try:
        artifact = load_from_file(path)
    except ModelError as exc:
        _load_failed = str(exc)
        logger.error("启用模型加载失败：%s", exc)
        return None, _load_failed

    if _active is None or _active.version != artifact.version:
        activate(artifact)
    return _active, None


def predict(
    features: dict[str, Any],
    *,
    artifact: ModelArtifact | None = None,
    top_n: int = 5,
) -> ModelPrediction:
    """线性推理 + 贡献度。

    步骤：取数（缺失用均值填充）→ log1p（如训练时启用）→ z-score →
    ``z = Σ w_i·z_i + b`` → ``sigmoid`` → 模型分 = round(100·p)。
    贡献度 ``contrib_i = w_i · z_i``，按绝对值排序取 top-N（正负号即方向）。
    """
    started = time.perf_counter()
    model = artifact or _active
    if model is None:
        return ModelPrediction(enabled=False, reason=_load_failed or "model_not_loaded")

    imputed: list[str] = []
    z_values: list[float] = []

    for index, name in enumerate(model.feature_names):
        raw = features.get(name)
        if raw is None or not isinstance(raw, (int, float)):
            # 缺失或非数值：直接取 z = 0（即训练均值所在的位置）并留痕。
            #
            # 这里**不能**先把均值填进去再走下面的变换：``mean`` 存的是**变换后**
            # 空间的均值，若再对它做一次 log1p，缺失样本会被推到分布之外，
            # 得到"看着正常但没道理"的分数。缺失即中性是唯一可解释的口径：
            # 模型对该特征没有信息 → 该维度不贡献分数。
            imputed.append(name)
            z_values.append(0.0)
            continue

        value = float(raw)
        if model.log1p:
            # log1p 需要非负输入；负值在业务上不该出现（计数/金额/比率都 ≥0），
            # 出现即视为脏数据，按 0 处理并留痕，避免 math domain error 打挂推理。
            if value < 0:
                imputed.append(name)
                value = 0.0
            value = math.log1p(value)

        std = model.std[index] or 1.0
        z_values.append((value - model.mean[index]) / std)

    linear = model.intercept + sum(
        weight * z for weight, z in zip(model.weights, z_values, strict=True)
    )
    probability = _sigmoid(linear)
    model_score = int(round(100 * probability))

    contributions = [
        {
            "feature_name": name,
            "feature_value": features.get(name),
            "contribution": round(model.weights[index] * z_values[index], 6),
            "direction": "up" if model.weights[index] * z_values[index] >= 0 else "down",
        }
        for index, name in enumerate(model.feature_names)
    ]
    contributions.sort(key=lambda item: abs(item["contribution"]), reverse=True)
    for rank, item in enumerate(contributions[:top_n], start=1):
        item["rank_no"] = rank

    return ModelPrediction(
        enabled=True,
        version=model.version,
        model_score=max(0, min(model_score, 100)),
        probability=round(probability, 6),
        contributions=contributions[:top_n],
        imputed_fields=sorted(set(imputed)),
        cost_ms=int((time.perf_counter() - started) * 1000),
    )


def _sigmoid(value: float) -> float:
    """数值稳定的 sigmoid。

    直接写 ``1/(1+exp(-x))`` 在 x 很负时会 exp 溢出（OverflowError），
    这里按符号分支，两个方向都不会溢出。
    """
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def artifact_path(version: str) -> Path:
    """按版本号拼模型文件路径（训练脚本与切换接口共用）。"""
    return ARTIFACT_DIR / f"model_{version}.json"


def reset() -> None:
    """清空当前模型（测试夹具用）。"""
    global _active, _load_failed
    _active = None
    _load_failed = None
