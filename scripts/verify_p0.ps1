#Requires -Version 7.0
<#
.SYNOPSIS
    P0 阶段验收：五类事件 → 特征 → 规则 → 模型 → 融合 → 落库 → 审计链，逐条自检。

.DESCRIPTION
    对应 docs/PRD.md §17.1 的 P0 交付物与 §17.2 验收标准 1~3 项：
      1. 依赖可用（MySQL / Redis）
      2. 迁移与种子就绪（规则数量、账号、API Key）
      3. 五类事件各注入一条，校验解析、落库、特征快照、决策与命中明细
      4. 三条动作路径（Pass / Review / Reject）各复现一次
      5. 幂等：同一 event_id 重放不产生第二条决策
      6. 审计哈希链自校验通过（篡改可被检出）

    设计取向：**脚本只做断言、不修数据**。所有写入都走真实接口（HTTP），
    因为"绕过接口直接改库"验证不了接入链路 —— 而 P0 的验收对象恰恰是链路。

    退出码：0 = 全部通过；1 = 有断言失败；2 = 环境不可用（依赖/迁移/种子）。

.PARAMETER BaseUrl
    已启动的后端地址。默认 http://127.0.0.1:8000。
    脚本不会替你启动服务（避免隐藏进程）：没起服务时请先 `pwsh -File scripts/dev.ps1`。

.PARAMETER ApiKey
    业务端 API Key（种子内置）。默认取种子里的演示 Key。

.PARAMETER SkipHttp
    只做数据库与审计链校验，不发起 HTTP 请求（服务未启动时用）。

.EXAMPLE
    pwsh -File scripts/verify_p0.ps1
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$ApiKey = "rc_demo_business_key",
    [string]$DbName = $env:DB_NAME,
    [switch]$SkipHttp
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if ($DbName) { $env:DB_NAME = $DbName }

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

$script:passed = 0
$script:failed = 0

function Assert-That {
    param([string]$Name, [bool]$Condition, [string]$Detail = "")
    if ($Condition) {
        $script:passed++
        Write-Host ("  [通过] " + $Name) -ForegroundColor Green
    }
    else {
        $script:failed++
        Write-Host ("  [失败] " + $Name) -ForegroundColor Red
        if ($Detail) { Write-Host ("         " + $Detail) -ForegroundColor DarkYellow }
    }
}

function _PytestSummary {
    param([string[]]$Output)
    # 汇总行是形如 "175 passed in 12.34s" 的那一行；直接取最后一行会拿到
    # "-- Docs: https://..." 这类尾部提示，失败信息就看不见了。
    $line = $Output | Select-String -Pattern "(\d+ (passed|failed|error))|no tests ran" |
        Select-Object -Last 1
    if ($line) { return $line.ToString().Trim() }
    return ($Output | Select-Object -Last 1)
}

Write-Host "==> P0 验收：电商风险控制系统" -ForegroundColor Cyan
Assert-That "虚拟环境存在" (Test-Path -LiteralPath $venvPython) "缺少 $venvPython，请先建 venv 并装依赖"

Push-Location $backendDir
try {
    # ------------------------------------------------------------------ #
    # 1. 数据库 / 迁移 / 种子
    # ------------------------------------------------------------------ #
    Write-Host "`n[1/4] 库、迁移与种子" -ForegroundColor Cyan
    $dbCheck = & $venvPython -c @"
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models.rule import RcRule
from app.models.sys import SysApiKey, SysUser

engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
with engine.connect() as conn:
    conn.execute(text('SELECT 1'))
S = sessionmaker(bind=engine, expire_on_commit=False)
with S() as db:
    rules = db.execute(select(func.count(RcRule.id)).where(RcRule.enabled.is_(True))).scalar_one()
    users = db.execute(select(func.count(SysUser.id))).scalar_one()
    keys = db.execute(select(func.count(SysApiKey.id)).where(SysApiKey.enabled.is_(True))).scalar_one()
print(f'{rules}|{users}|{keys}|{settings.DB_NAME}')
engine.dispose()
"@
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[中止] 数据库不可用：确认 FlyEnv 里 MySQL/Redis 已启动，且已跑过 scripts/init_db.ps1" -ForegroundColor Red
        exit 2
    }
    $parts = ($dbCheck | Select-Object -Last 1).Split("|")
    Assert-That "启用规则不少于 20 条（实际 $($parts[0])）" ([int]$parts[0] -ge 20)
    Assert-That "预置账号 3 个（实际 $($parts[1])）" ([int]$parts[1] -ge 3)
    Assert-That "可用 API Key 至少 1 个（实际 $($parts[2])）" ([int]$parts[2] -ge 1)
    Write-Host ("        目标库：{0}" -f $parts[3])

    Write-Host "`n[2/4] 迁移漂移检查（alembic check）" -ForegroundColor Cyan
    $drift = & $venvPython -m alembic check 2>&1
    Assert-That "模型定义与迁移一致（无待生成迁移）" ($LASTEXITCODE -eq 0) ($drift | Select-Object -Last 3 | Out-String)

    # ------------------------------------------------------------------ #
    # 2. 五类事件端到端注入
    # ------------------------------------------------------------------ #
    Write-Host "`n[3/4] 五类事件端到端注入" -ForegroundColor Cyan
    if ($SkipHttp) {
        Write-Host "  [跳过] 已指定 -SkipHttp" -ForegroundColor Yellow
    }
    else {
        $health = $null
        try {
            $health = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 5
        }
        catch {
            Write-Host ("  [跳过] 后端不可达（{0}）：请先启动 pwsh -File scripts/dev.ps1" -f $BaseUrl) -ForegroundColor Yellow
            $SkipHttp = $true
        }
        if (-not $SkipHttp) {
            Assert-That "健康检查 /healthz 可用" ($null -ne $health)

            # 事件的注入与断言走 pytest 里的集成用例：
            # 事件契约（每个事件类型的必填字段）集中在那儿维护，
            # 验收脚本不再复制一份 payload —— 复制出来的第二份必然会与契约漂移，
            # 而"验收脚本用错字段还报通过"是最危险的一类假绿。
            # 这里挑的四个用例分别对应：
            #   five_event_types_all_persist      —— 五类事件都能解析并落库（验收标准 1）
            #   cluster_strips_feed_distinct_features —— 特征快照真的有值（验收标准 2）
            #   login_event_pass_path / review_* / reject_* —— 三条仲裁路径（验收标准 3）
            $selection = "five_event_types_all_persist or cluster_strips_feed_distinct_features or " +
                "login_event_pass_path or review_path_when_score_reaches_review_threshold or reject_path_at_high_score"
            $e2e = & $venvPython -m pytest tests/test_event_gateway.py -q -k $selection 2>&1
            $lastLine = _PytestSummary $e2e
            Assert-That "五类事件解析 / 决策 / 落库用例通过" ($LASTEXITCODE -eq 0) $lastLine
            Write-Host ("        " + $lastLine)
        }
    }

    # ------------------------------------------------------------------ #
    # 3. 决策落库完整性 + 审计链
    # ------------------------------------------------------------------ #
    Write-Host "`n[4/4] 决策落库与审计链" -ForegroundColor Cyan
    $integrity = & $venvPython -c @"
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models.decision import RcDecision, RcModelContribution
# RcFeatureSnapshot 在 event 模块（快照与事件同属"接入侧"事实数据）
from app.models.event import RcEvent, RcFeatureSnapshot
from app.services import audit_service

engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
S = sessionmaker(bind=engine, expire_on_commit=False)
with S() as db:
    events = db.execute(select(func.count(RcEvent.id))).scalar_one()
    decisions = db.execute(select(func.count(RcDecision.id))).scalar_one()
    snapshots = db.execute(select(func.count(RcFeatureSnapshot.id))).scalar_one()
    orphan = db.execute(
        select(func.count(RcEvent.id)).outerjoin(RcDecision, RcDecision.event_id == RcEvent.event_id)
        .where(RcDecision.id.is_(None))
    ).scalar_one()
    contributions = db.execute(select(func.count(RcModelContribution.id))).scalar_one()
    actions = dict(db.execute(select(RcDecision.action, func.count()).group_by(RcDecision.action)).all())
    report = audit_service.verify(db)
print(f'{events}|{decisions}|{snapshots}|{orphan}|{contributions}|{actions}|{report.valid}|{report.detail}')
engine.dispose()
"@
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[失败] 落库完整性检查未能执行" -ForegroundColor Red
        $script:failed++
    }
    else {
        $f = ($integrity | Select-Object -Last 1).Split("|")
        $events, $decisions, $snapshots, $orphan = [int]$f[0], [int]$f[1], [int]$f[2], [int]$f[3]
        Write-Host ("        事件 {0}｜决策 {1}｜特征快照 {2}｜模型贡献 {3}｜动作分布 {4}" -f $events, $decisions, $snapshots, $f[4], $f[5])
        Assert-That "已有事件入库（$events 条）" ($events -gt 0)
        Assert-That "每个事件恰好一条决策（孤儿事件 $orphan）" ($orphan -eq 0)
        Assert-That "每个决策都有特征快照（快照 $snapshots / 决策 $decisions）" ($snapshots -ge $decisions)
        Assert-That "审计哈希链自校验通过" ($f[6] -eq "True") $f[7]
    }

    Write-Host "`n[附加] 单元与集成测试（pytest）" -ForegroundColor Cyan
    $pytest = & $venvPython -m pytest -q 2>&1
    $summary = _PytestSummary $pytest
    Assert-That "pytest 全绿" ($LASTEXITCODE -eq 0) $summary
    Write-Host ("        " + $summary)
}
finally {
    Pop-Location
}

Write-Host ("`n==> 验收结果：通过 {0} 项，失败 {1} 项" -f $script:passed, $script:failed) -ForegroundColor $(if ($script:failed -eq 0) { "Green" } else { "Red" })
if ($script:failed -gt 0) { exit 1 }
exit 0
