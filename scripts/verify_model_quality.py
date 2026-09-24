"""模型质量核对（只读）：给数据集**全量样本**打分，对照训练脚本的验证集指标。

用法（在仓库根目录执行）::

    backend/.venv/Scripts/python.exe scripts/verify_model_quality.py
    backend/.venv/Scripts/python.exe scripts/verify_model_quality.py --db-name risk_control_dataset

**为什么要有它**：``train_model.py`` 的 AUC/KS 只来自**时间切分的验证段**（后 30%），
而验收清单里还引用了两个"全样本"口径（事件级 AUC、账号级 AUC）与"作弊/正常事件的
分档占比"。这两组数如果只靠一次性的诊断脚本算出来，就无法复跑 —— 数字一旦漂移
（换了产物、改了特征），没人能发现。此脚本把这两组数的计算固定下来，
让验收清单里的每一个数字都有对应入口。

**它不做的事**：不训练、不写库、不切换启用版本。只读数据集库 + 读取产物文件，
因此可以随时在演示机上跑（不会污染演示数据）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

# 与 train_model.py 一致：先定位 backend/，再在任何 app 导入之前设置环境变量。
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

ARTIFACT_DIR = BACKEND_DIR / "models_artifacts"
DATASET_DB_NAME = "risk_control_dataset"

#: 分档口径与融合仲裁一致（>=60 判中风险，>=80 判高风险）。
SCORE_BANDS = (60, 80)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模型质量核对（只读）")
    parser.add_argument("--db-name", default=DATASET_DB_NAME, help="带标签的数据集库名")
    parser.add_argument("--version", default="v1", help="产物版本号（读 models_artifacts）")
    parser.add_argument("--artifact", default=None, help="直接指定产物路径（优先于 --version）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    import os

    os.environ["DB_NAME"] = args.db_name
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    import numpy as np
    from sklearn.metrics import roc_auc_score
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.models.event import RcEvent, RcFeatureSnapshot
    from app.services import model_engine

    artifact_path = Path(args.artifact) if args.artifact else ARTIFACT_DIR / f"model_{args.version}.json"
    if not artifact_path.exists():
        print(f"产物不存在：{artifact_path}", file=sys.stderr)
        return 2

    artifact = model_engine.load_from_file(artifact_path)

    engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as db:
        rows = db.execute(
            select(RcEvent.user_id, RcEvent.is_cheat, RcFeatureSnapshot.features)
            .join(RcFeatureSnapshot, RcFeatureSnapshot.event_id == RcEvent.event_id)
            .where(RcEvent.is_cheat.isnot(None))
            .order_by(RcEvent.occurred_at, RcEvent.id)
        ).all()
    engine.dispose()

    if not rows:
        print(f"库 {settings.DB_NAME} 里没有带标签的样本，请先跑 scripts/gen_dataset.py", file=sys.stderr)
        return 2

    event_scores: list[float] = []
    event_labels: list[int] = []
    # 账号级口径：一个账号的分数取它所有事件的**最高分**。风控的处置单位是账号，
    # 因此这个数比事件级更贴近业务（一个账号只要有一条事件被识别，就该被拦下）。
    account_score: dict[str, float] = defaultdict(float)
    account_label: dict[str, int] = {}

    for user_id, is_cheat, payload in rows:
        snapshot = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        prediction = model_engine.predict(snapshot or {}, artifact=artifact)
        score = float(prediction.model_score)
        event_scores.append(score)
        event_labels.append(1 if is_cheat else 0)
        account_score[user_id] = max(account_score[user_id], score)
        account_label[user_id] = 1 if is_cheat else 0

    users = list(account_score)
    cheat = [s for s, y in zip(event_scores, event_labels, strict=True) if y == 1]
    normal = [s for s, y in zip(event_scores, event_labels, strict=True) if y == 0]

    print("===== 模型质量核对（全样本，只读）=====")
    print(f"  产物 {artifact_path.name}｜库 {settings.DB_NAME}｜样本 {len(event_labels)} 条")
    print(f"  事件级 AUC {roc_auc_score(event_labels, event_scores):.4f}")
    print(
        f"  账号级 AUC {roc_auc_score([account_label[u] for u in users], [account_score[u] for u in users]):.4f}"
        f"（账号 {len(users)} 个，取账号内最高分）"
    )
    for name, group in (("作弊事件", cheat), ("正常事件", normal)):
        if not group:
            continue
        hits = "、".join(f"≥{band} {np.mean([s >= band for s in group]):.1%}" for band in SCORE_BANDS)
        print(f"  {name}：均分 {np.mean(group):.1f}｜{hits}")

    print("\n提示：这两个 AUC 含训练集样本，只用于核对「模型确实在用特征」，")
    print("      验收口径请以 train_model.py 的时间切分验证集指标为准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
