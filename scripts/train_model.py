"""模型训练脚本（docs/PRD.md §8.3）。

流程：读数据集库 → 构造样本（事件 × 特征快照 × 标签）→ log1p + z-score →
逻辑回归（class_weight=balanced）→ 评估 AUC/KS/precision@100 → 产出模型文件 →
登记 rc_model_version（可选直接启用）。

用法（在仓库根目录执行）::

    backend/.venv/Scripts/python.exe scripts/train_model.py                 # 默认 v1，自动启用
    backend/.venv/Scripts/python.exe scripts/train_model.py --version v2 --no-activate

**为什么训练要连"数据集库"而不是开发库**：标签只存在于生成器写出的那批事件上
（``rc_event.is_cheat`` 非空）。连错库会得到"0 条样本"，脚本会明确报错而不是
产出一个空模型 —— 空模型比没有模型更危险：它会让线上所有事件拿到接近 0 的分，
看上去"模型在工作"，实际什么都没算。

**评估口径**：按时间 7:3 切分（前 70% 训练、后 30% 验证）。不能用随机切分 ——
风控特征是滑窗统计量，随机切分会让"未来"的样本进入训练集，
验证指标被信息泄漏抬高（同一账号的相邻事件被分到两边，模型等于背过答案）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path

# 与 gen_dataset.py 一致：先定位 backend/，再在任何 app 导入之前设置环境变量。
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

DATASET_DB_NAME = "risk_control_dataset"
ARTIFACT_DIR = BACKEND_DIR / "models_artifacts"

# 20 维模型输入（docs/PRD.md §8.3 第 2 步）。
#
# 与 PRD 列表的两处名字差异按**特征注册表的实际键名**为准（训练脚本必须引用
# 真实存在的键，否则线上推理会把它当作缺失特征、用均值填充，权重静默失效）：
#   * PRD 写 address_phone_share_cnt_7d，注册表是 address_phone_share_cnt（无窗口）
#   * PRD 写 night_activity_ratio_7d，注册表是 night_activity_ratio（当前为二值口径）
FEATURE_NAMES: tuple[str, ...] = (
    "device_account_cnt_24h",
    "device_new_account_ratio_24h",
    "device_coupon_cnt_1h",
    "device_env_risk",
    "ip_account_cnt_24h",
    "ip_coupon_cnt_1h",
    "ip_is_datacenter",
    "ip_region_mismatch",
    "user_coupon_cnt_1h",
    "user_coupon_amount_24h",
    "user_order_cnt_1h",
    "user_refund_cnt_24h",
    "user_refund_rate_24h",
    "user_refund_amount_24h",
    "user_first_order_refund",
    "address_account_cnt_7d",
    "address_refund_rate_7d",
    "address_phone_share_cnt",
    "account_age_days",
    "night_activity_ratio",
)

# 缺失率告警线：某个特征在样本里缺失过半，说明"特征注册表声明了但引擎没产出"，
# 训练出来的权重会建在均值填充之上（等于没学）。宁可报警也不要静默接受。
MISSING_RATIO_WARN = 0.5

# 单特征判别力的可疑线：某个特征**单独**就能把两类样本分开时，模型的高分不是
# 判别力，而是生成器的笔误（典型是"作弊账号刚好都用了同一个地址/设备"）。
# 逻辑回归的 |权重| 越大，说明越接近"看这一个特征就够了"。
LEAKY_WEIGHT_WARN = 3.0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="风控模型训练（PRD §8.3）")
    parser.add_argument("--version", default="v1", help="模型版本号（默认 v1）")
    parser.add_argument("--db-name", default=DATASET_DB_NAME, help="数据集库名")
    parser.add_argument("--output", default=None, help="产物路径（默认 models_artifacts/model_<版本>.json）")
    parser.add_argument("--valid-ratio", type=float, default=0.3, help="验证集比例（按时间取尾部）")
    parser.add_argument("--min-samples", type=int, default=200, help="样本数下限，低于此值拒绝训练")
    parser.add_argument("--no-activate", action="store_true", help="只登记版本，不切换启用状态")
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="不训练：把已有产物登记到目标库（用于把数据集库训练出的模型部署到开发/演示库）",
    )
    parser.add_argument("--remark", default="", help="版本备注（写入 rc_model_version.remark）")
    return parser.parse_args(argv)


def _configure_env(args: argparse.Namespace) -> None:
    os.environ["DB_NAME"] = args.db_name
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _load_samples(db, feature_names: tuple[str, ...]):
    """按时间顺序读出 (特征矩阵, 标签)；缺失特征用 NaN 表示。"""
    import numpy as np
    from sqlalchemy import select

    from app.models.event import RcEvent, RcFeatureSnapshot

    rows = db.execute(
        select(RcEvent.occurred_at, RcEvent.is_cheat, RcFeatureSnapshot.features)
        .join(RcFeatureSnapshot, RcFeatureSnapshot.event_id == RcEvent.event_id)
        .where(RcEvent.is_cheat.isnot(None))
        .order_by(RcEvent.occurred_at, RcEvent.id)
    ).all()

    features = np.full((len(rows), len(feature_names)), np.nan, dtype=float)
    labels = np.zeros(len(rows), dtype=int)
    missing = np.zeros(len(feature_names), dtype=int)
    for row_index, (_occurred_at, is_cheat, payload) in enumerate(rows):
        snapshot = payload
        if isinstance(snapshot, (str, bytes)):
            snapshot = json.loads(snapshot)
        snapshot = snapshot or {}
        for col, name in enumerate(feature_names):
            value = snapshot.get(name)
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, (int, float)):
                features[row_index, col] = float(value)
            else:
                missing[col] += 1
        labels[row_index] = 1 if is_cheat else 0
    return features, labels, missing


def _to_z(features, *, log1p: bool, mean=None, std=None):
    """原始特征矩阵 → z 空间，缺失格子取 **z = 0**。

    这是训练与推理的**唯一定口**，与 ``model_engine.predict`` 逐字对齐：

    1. NaN 原地保留先做 log1p（负值夹到 0，与推理侧"负值算脏数据按 0 处理"一致）；
    2. ``mean`` / ``std`` 为 None 时按列计算**变换后**空间的统计量（忽略 NaN），
       否则复用传入的统计量做变换（验证集必须复用训练集的数）；
    3. 最后把仍是 NaN 的位置（原本缺失、或整列无数据）置 0。

    **为什么不能"先用原始均值填缺失、再 log1p、再 z-score"**：填充值经过
    log1p 与 z-score 之后落在分布之外，与线上"缺失即中性（z = 0）"不等价 ——
    训练学到的偏移在推理时不成立，分数会系统性偏离。整列全 NaN 时均值取 0、
    标准差取 1，缺失格同样取 z = 0（该维没有信息，就不参与打分）。
    """
    import numpy as np

    values = np.log1p(np.clip(features, 0.0, None)) if log1p else features.copy()
    if mean is None or std is None:
        with warnings.catch_warnings(), np.errstate(invalid="ignore"):
            # 整列全 NaN（这一维一条数据都没有）不是错误，只是"没有信息"，
            # 不必让 numpy 的 "Mean of empty slice" 警告污染训练日志。
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(values, axis=0)
            std = np.nanstd(values, axis=0)
        mean = np.where(np.isnan(mean), 0.0, mean)
        std = np.where(np.isnan(std) | (std == 0), 1.0, std)  # 常数列：避免除零
    z = (values - mean) / std
    return np.nan_to_num(z, nan=0.0, posinf=0.0, neginf=0.0), mean, std


def _metrics(y_true, scores, *, top_n: int) -> dict:
    """AUC / KS / precision@N。"""
    from sklearn.metrics import roc_auc_score, roc_curve

    auc = float(roc_auc_score(y_true, scores)) if len(set(y_true.tolist())) > 1 else float("nan")
    fpr, tpr, _thresholds = roc_curve(y_true, scores)
    ks = float(max(tpr - fpr))
    order = scores.argsort()[::-1][:top_n]
    hits = int(y_true[order].sum())
    return {
        "auc": round(auc, 4),
        "ks": round(ks, 4),
        f"precision_at_{top_n}": round(hits / max(1, len(order)), 4),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_env(args)

    import numpy as np
    import sklearn
    from sklearn.ensemble import IsolationForest
    from sklearn.linear_model import LogisticRegression
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.core.timeutil import utcnow
    from app.models.decision import RcModelVersion
    from app.services import audit_service
    from app.services.feature_engine import FEATURE_REGISTRY, DEFAULT_WINDOWS

    # 特征名对照注册表校验：写错名字的特征在线上会被当缺失值处理（静默失效），
    # 这类错误不会让训练报错，只让模型悄悄变差，所以必须在训练前拦住。
    known = {key for spec in FEATURE_REGISTRY for key in spec.keys(DEFAULT_WINDOWS)}
    unknown = [name for name in FEATURE_NAMES if name not in known]
    if unknown:
        print(f"特征名不在注册表中：{unknown}", file=sys.stderr)
        return 2

    engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as db:
        if args.register_only:
            code = _register_from_artifact(
                db,
                args,
                feature_names=FEATURE_NAMES,
                known=known,
                RcModelVersion=RcModelVersion,
                audit=audit_service,
            )
            engine.dispose()
            return code
        features, labels, missing = _load_samples(db, FEATURE_NAMES)
        total = len(labels)

        if total < args.min_samples:
            print(
                f"样本数 {total} 少于下限 {args.min_samples}：请先跑 "
                f"scripts/gen_dataset.py（数据库 {settings.DB_NAME}）",
                file=sys.stderr,
            )
            return 2
        if labels.sum() == 0 or labels.sum() == total:
            print(f"标签全为同一类（正样本 {int(labels.sum())}/{total}），无法训练", file=sys.stderr)
            return 2

        split = int(total * (1 - args.valid_ratio))
        train_x_raw, valid_x_raw = features[:split], features[split:]
        train_y, valid_y = labels[:split], labels[split:]
        if train_y.sum() == 0 or valid_y.sum() == 0:
            # 时间切分的固有风险：作弊账号若只出现在后半段，训练集里就没有正样本。
            # 这里直接失败而不是继续 —— 继续会得到一个"永远预测正常"的模型。
            print(
                f"按时间切分后某一侧没有正样本（训练 {int(train_y.sum())} / "
                f"验证 {int(valid_y.sum())}）：请检查数据集的时间分布",
                file=sys.stderr,
            )
            return 2

        # 标准化统计量必须来自**训练集**：用全体样本会把验证集信息带进模型
        #
        # 缺失口径见 _to_z：缺失格一律取 z = 0，与线上 model_engine.predict
        # 完全等价。口径一致性优先于"填充看起来更讲究"—— 两边不一致时，
        # 训练侧算出的权重在线上就是错的，而且不会报错。
        train_x, mean, std = _to_z(train_x_raw, log1p=True)
        valid_x = _to_z(valid_x_raw, log1p=True, mean=mean, std=std)[0]

        model = LogisticRegression(
            class_weight="balanced",  # 正样本只占 6%~8%，不配平会退化成"全判正常"
            max_iter=1000,
            solver="lbfgs",
        )
        model.fit(train_x, train_y)

        valid_scores = model.predict_proba(valid_x)[:, 1]
        metrics = _metrics(valid_y, valid_scores, top_n=100)
        metrics["samples"] = total
        metrics["train_samples"] = int(split)
        metrics["valid_samples"] = int(total - split)
        metrics["positive_rate"] = round(float(labels.mean()), 4)
        metrics["valid_positive_rate"] = round(float(valid_y.mean()), 4)
        metrics["missing_rate"] = {
            name: round(float(count) / total, 4)
            for name, count in zip(FEATURE_NAMES, missing, strict=True)
            if count
        }

        # 对照指标（PRD §8.3 第 3 步）：孤立森林只用于对比"线性模型够不够用"，
        # 不参与线上决策 —— 它的输出没有可解释权重，与"贡献度 top5"的产品要求冲突。
        iforest = IsolationForest(n_estimators=100, random_state=42, contamination="auto")
        iforest.fit(train_x)
        iforest_scores = -iforest.score_samples(valid_x)
        metrics["iforest_auc"] = _metrics(valid_y, iforest_scores, top_n=100)["auc"]

        trained_at = utcnow()
        payload = {
            "version": args.version,
            "trained_at": trained_at.replace(microsecond=0).isoformat() + "Z",
            "feature_names": list(FEATURE_NAMES),
            "log1p": True,
            "mean": [round(float(value), 8) for value in mean],
            "std": [round(float(value), 8) for value in std],
            "weights": [round(float(value), 8) for value in model.coef_[0]],
            "intercept": round(float(model.intercept_[0]), 8),
            "metrics": metrics,
            "label_definition": "rc_event.is_cheat：1=作弊账号事件，0=正常账号事件（合成数据集标签）",
            "trained_with": f"scikit-learn {sklearn.__version__}",
        }

        output = Path(args.output) if args.output else ARTIFACT_DIR / f"model_{args.version}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        relative_path = str(output.relative_to(BACKEND_DIR)) if output.is_absolute() else str(output)
        _register_version(
            db,
            version=args.version,
            file_path=relative_path,
            metrics=metrics,
            sample_count=total,
            trained_at=trained_at,
            activate=not args.no_activate,
            remark=args.remark,
            RcModelVersion=RcModelVersion,
            audit=audit_service,
        )

        # 权重可读性检查：全为 0 的权重说明该特征在训练集里没有区分度
        # （常见原因是特征恒为常量），打印出来便于人工核对"模型到底看了什么"。
        weights = payload["weights"]
        print("\n===== 训练结果 =====")
        print(f"样本 {total} 条（训练 {split} / 验证 {total - split}），正样本占比 {labels.mean():.2%}")
        print(f"  AUC {metrics['auc']}｜KS {metrics['ks']}｜precision@100 {metrics['precision_at_100']}")
        print(f"  对照：孤立森林 AUC {metrics['iforest_auc']}（不参与线上决策）")
        top = sorted(zip(FEATURE_NAMES, weights), key=lambda item: -abs(item[1]))[:5]
        print("  权重 Top5：" + "、".join(f"{name} {weight:+.3f}" for name, weight in top))
        print(f"  模型文件：{output}")
        print(f"  版本登记：{args.version}{'（已启用）' if not args.no_activate else '（未启用）'}")

    engine.dispose()

    warned = {
        name: ratio for name, ratio in metrics["missing_rate"].items()
        if ratio > MISSING_RATIO_WARN
    }
    if warned:
        print(f"\n⚠️ 以下特征缺失率过高，其权重建立在均值填充之上：{warned}")
        return 1
    # 泄漏自检：AUC 满分 + 某个特征权重极大 = 生成器把两类样本画得太开，
    # 模型学到的是"数据集特征"而不是"业务规律"。这类模型上线会立刻失效，
    # 同时也是答辩现场最容易被指出造假的地方（真实风控 AUC 达 1.0 不存在）。
    if metrics["auc"] >= 0.995 and max(abs(value) for value in weights) >= LEAKY_WEIGHT_WARN:
        print(
            "\n⚠️ 疑似标签泄漏：AUC 接近 1.0 且存在权重量级异常的特征，"
            "请核对作弊画像与正常画像是否在环境/地址维度完全分离"
        )
        return 1
    return 0


def _register_from_artifact(
    db,
    args: argparse.Namespace,
    *,
    feature_names: tuple[str, ...],
    known: set[str],
    RcModelVersion,
    audit,
) -> int:
    """把**已有产物**登记到当前目标库，不做训练（``--register-only``）。

    为什么需要这条路径：**训练连的是数据集库，推理连的是开发库**（两个库各有各的
    ``rc_model_version`` 表）。没有它，就得手工往开发库插一行并手写相对路径 ——
    而 ``file_path`` 一旦写错（比如填了绝对路径、或漏了 `models_artifacts/` 前缀），
    ``model_engine.load_active`` 会**静默降级**为"模型不可用"，
    决策照样返回 200，只是 ``model_score`` 恒为 0、贡献度一片空白。
    这种"看起来在跑、其实没跑"的状态必须在部署阶段就被拦住：
    这里会校验产物文件存在、特征名与注册表一致、且特征顺序与产物完全相同。
    """
    import numpy as np

    output = Path(args.output) if args.output else ARTIFACT_DIR / f"model_{args.version}.json"
    if not output.exists():
        print(f"模型产物不存在：{output}（先跑一次不带 --register-only 的训练）", file=sys.stderr)
        return 2
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"模型产物不可读：{exc}", file=sys.stderr)
        return 2

    required = {"version", "feature_names", "weights", "mean", "std", "intercept"}
    missing_keys = sorted(required - set(payload))
    if missing_keys:
        print(f"模型产物缺少必需字段：{missing_keys}", file=sys.stderr)
        return 2

    artifact_features = list(payload["feature_names"])
    if artifact_features != list(feature_names):
        # 顺序必须完全一致：推理端按位置对齐权重，顺序错一位就等价于
        # "把 A 特征的权重按在 B 特征上"，而结果依然是一个像模像样的分数。
        print(
            "产物特征与训练脚本的 FEATURE_NAMES 不一致（顺序敏感）：\n"
            f"  产物：{artifact_features}\n  脚本：{list(feature_names)}",
            file=sys.stderr,
        )
        return 2
    unknown = [name for name in artifact_features if name not in known]
    if unknown:
        print(f"产物含注册表之外的特征：{unknown}", file=sys.stderr)
        return 2

    for name in ("weights", "mean", "std"):
        if len(payload[name]) != len(artifact_features):
            print(f"{name} 长度与特征数不符", file=sys.stderr)
            return 2

    sample_count = int(payload.get("metrics", {}).get("samples") or 0)
    relative_path = str(output.relative_to(BACKEND_DIR)) if output.is_absolute() else str(output)
    _register_version(
        db,
        version=args.version,
        file_path=relative_path,
        metrics=dict(payload.get("metrics") or {}),
        sample_count=sample_count,
        trained_at=datetime.now(),
        activate=not args.no_activate,
        remark=args.remark or "由 --register-only 从已有产物登记",
        RcModelVersion=RcModelVersion,
        audit=audit,
    )
    metrics = payload.get("metrics") or {}
    print("\n===== 版本登记（未训练）=====")
    print(f"  版本 {args.version}｜样本 {sample_count}｜产物 {output}")
    if metrics:
        print(
            f"  AUC {metrics.get('auc')}｜KS {metrics.get('ks')}｜"
            f"precision@100 {metrics.get('precision_at_100')}"
        )
    print(f"  启用状态：{'已启用' if not args.no_activate else '未启用'}")
    _ = np  # 保持与训练路径一致的依赖存在性检查（缺少 numpy 时提前暴露）
    return 0


def _register_version(
    db,
    *,
    version: str,
    file_path: str,
    metrics: dict,
    sample_count: int,
    trained_at: datetime,
    activate: bool,
    remark: str,
    RcModelVersion,
    audit,
) -> None:
    """登记（或更新）模型版本；激活时保证"同一时刻只有一个启用版本"。"""
    from sqlalchemy import select, update

    row = db.execute(
        select(RcModelVersion).where(RcModelVersion.version == version)
    ).scalar_one_or_none()
    before = None
    if row is None:
        row = RcModelVersion(version=version)
        db.add(row)
        action = "model_version_create"
    else:
        before = {"file_path": row.file_path, "metrics": row.metrics}
        action = "model_version_update"
    row.file_path = file_path
    row.feature_names = list(FEATURE_NAMES)
    row.metrics = metrics
    row.sample_count = sample_count
    row.trained_at = trained_at
    row.remark = remark or row.remark
    if activate:
        # 先全部置为未启用，再启用本条：两步在同一次提交里，
        # 不会出现"0 个启用版本"或"2 个启用版本"的中间态。
        db.execute(update(RcModelVersion).values(active=False))
        row.active = True
    db.flush()
    audit.write(
        db,
        action=action,
        actor_name="train_model.py",
        target_type="model_version",
        target_id=version,
        before=before,
        after={"file_path": file_path, "metrics": metrics, "active": bool(row.active)},
        reason=remark or "离线训练产出",
    )
    db.commit()
    print(f"版本已登记：{version}（active={bool(row.active)}）")


if __name__ == "__main__":
    raise SystemExit(main())
