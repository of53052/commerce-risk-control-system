"""种子数据装配层。

约定：
- 每个种子模块暴露 ``NAME``（字符串）与 ``run(db) -> int``（返回写入/更新的行数），
  并在此处登记到 ``SEED_MODULES``。``scripts/init_db.ps1`` 只调用 ``run_all``。
- **幂等**：全部按业务自然键 upsert（存在则更新、不存在则插入），
  因此 ``init_db.ps1`` 可以重复执行而不会报错、也不会产生重复数据
  （docs/PRD.md §17.1 的明确要求）。
- **只增改、不删除**：种子不清理历史数据。要彻底重来请重建库
  （docs/ARCHITECTURE.md §13：数据回滚 = 重建独立库）。
- 依赖方向是 ``seeds → services/models``：种子是边缘适配层，
  它可以引用业务层（如配置注册表），业务层不反向依赖种子。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from sqlalchemy.orm import Session

from app.seeds import accounts, api_keys, configs


@dataclass(frozen=True)
class SeedResult:
    name: str
    affected: int


# 登记顺序即执行顺序：配置 → 账号 → API Key。
# 后续 P0 步骤会在此追加规则种子（依赖表达式引擎先定型），P1 追加名单种子。
SEED_MODULES: tuple[ModuleType, ...] = (configs, accounts, api_keys)


def run_all(db: Session) -> list[SeedResult]:
    """执行全部种子并统一提交。

    统一提交而非各自提交：种子之间没有外部副作用，
    一次事务要么整体成功、要么整体回滚，不留"账号建了但配置没建"的中间态。
    """
    results: list[SeedResult] = []
    for module in SEED_MODULES:
        affected = module.run(db)
        results.append(SeedResult(name=module.NAME, affected=affected))
    db.commit()
    return results


__all__ = ["SEED_MODULES", "SeedResult", "run_all"]

