#Requires -Version 7.0
<#
.SYNOPSIS
    P1 阶段验收：中高风险自动建案 → 工作台看证据 → 接手 → 处置联动 → 归档，逐条自检。

.DESCRIPTION
    对应 docs/PRD.md §17.1 的 P1 交付物与 §17.2 验收标准 4~5 项：
      1. 迁移已到 P1（rc_case / rc_case_event / rc_case_action / rc_case_action_item 四表就绪）
      2. 决策动作为 Review/Reject 时**真的**建案（case_no 非空且落库、写回决策行）
      3. 同主体同场景窗口内二次触发**合案**（不新建案件）
      4. 案件接口：列表 → 详情（画像/单据/特征/图谱/证据）→ 接手 → 处置
      5. 处置联动落到业务表：订单被取消、账号进黑名单、业务用户置黑
      6. 处置与归档入审计，且哈希链自校验通过

    与 verify_p0.ps1 的分工：P0 脚本证明"决策链路正确"，P1 脚本证明"人工闭环可用"。

    脚本做事的方式：
      * **业务动作走模拟业务端**（app/simulator 的 BusinessSimulator，进程内调用），
        因为只有它会把"风控放行"翻译成真实的业务单据（未拦截的订单才会落 biz_order）；
      * **运营动作走 HTTP 接口**（登录 / 列表 / 详情 / 接手 / 处置 / 归档），
        因为验收对象就是这些接口的行为与错误码；
      * **断言用只读查询**（SQLAlchemy 只读会话），不改任何数据。

    退出码：0 = 全部通过；1 = 有断言失败；2 = 环境不可用（依赖/迁移/服务未启动）。

.PARAMETER BaseUrl
    已启动的后端地址。默认 http://127.0.0.1:8000。
    脚本不会替你启动服务（避免隐藏进程）：没起服务时请先 `pwsh -File scripts/dev.ps1`。

.PARAMETER SkipHttp
    只做数据库侧校验（迁移 + 四表 + 审计链），不发起 HTTP 请求，也不注入事件。

.EXAMPLE
    pwsh -File scripts/verify_p1.ps1
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
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

function Invoke-Json {
    <#
        统一封装 HTTP 调用：返回 (StatusCode, Body)。
        刻意**不抛异常**：验收脚本要区分"接口返回了 4xx"与"服务没起"，
        用 try/catch 包住会让两者混在一起，断言信息就说不清到底哪里不对。
    #>
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Headers = @{},
        [object]$Body = $null
    )
    $params = @{
        Method      = $Method
        Uri         = ($BaseUrl.TrimEnd("/") + $Path)
        Headers     = $Headers
        ContentType = "application/json; charset=utf-8"
        TimeoutSec  = 15
    }
    if ($null -ne $Body) { $params["Body"] = ($Body | ConvertTo-Json -Depth 8 -Compress) }
    try {
        $response = Invoke-WebRequest @params -SkipHttpErrorCheck
    }
    catch {
        return [pscustomobject]@{ StatusCode = 0; Body = $null; Error = $_.Exception.Message }
    }
    $parsed = $null
    if ($response.Content) {
        try { $parsed = $response.Content | ConvertFrom-Json } catch { $parsed = $null }
    }
    return [pscustomobject]@{ StatusCode = [int]$response.StatusCode; Body = $parsed; Error = $null }
}

Write-Host "==> P1 验收：案件流转与处置联动" -ForegroundColor Cyan
Assert-That "虚拟环境存在" (Test-Path -LiteralPath $venvPython) "缺少 $venvPython，请先建 venv 并装依赖"
if ($script:failed -gt 0) { exit 2 }

Push-Location $backendDir
try {
    # ------------------------------------------------------------------ #
    # 1. 迁移与表结构
    # ------------------------------------------------------------------ #
    Write-Host "`n[1/5] P1 迁移与案件四表" -ForegroundColor Cyan
    $migrate = & $venvPython -c @"
from sqlalchemy import create_engine, inspect, text
from app.core.config import settings
engine = create_engine(settings.db_url, pool_pre_ping=True)
inspector = inspect(engine)
tables = set(inspector.get_table_names())
required = {"rc_case", "rc_case_event", "rc_case_action", "rc_case_action_item"}
missing = sorted(required - tables)
with engine.connect() as conn:
    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    cases = conn.execute(text("SELECT COUNT(*) FROM rc_case")).scalar() if "rc_case" in tables else -1
print(f"missing={missing}|version={version}|cases={cases}|db={settings.DB_NAME}")
"@
    $line = ($migrate | Select-Object -Last 1).ToString()
    Assert-That "案件四表已建" ($line -match "missing=\[\]") $line
    Assert-That "alembic 版本表可用" ($line -match "version=\w+") $line
    Write-Host ("         " + $line) -ForegroundColor DarkGray

    # ------------------------------------------------------------------ #
    # 2. 迁移漂移检查
    # ------------------------------------------------------------------ #
    Write-Host "`n[2/5] 迁移漂移检查（alembic check）" -ForegroundColor Cyan
    $check = & $venvPython -m alembic check 2>&1
    Assert-That "模型定义与迁移一致（无待生成迁移）" ($LASTEXITCODE -eq 0) ($check | Select-Object -Last 1)

    if ($SkipHttp) {
        Write-Host "`n[3/5] 事件注入与建案" -ForegroundColor Cyan
        Write-Host "  [跳过] 已指定 -SkipHttp" -ForegroundColor DarkYellow
        Write-Host "`n[4/5] 案件接口闭环" -ForegroundColor Cyan
        Write-Host "  [跳过] 已指定 -SkipHttp" -ForegroundColor DarkYellow
    }
    else {
        # -------------------------------------------------------------- #
        # 3. 业务动作 → 建案（走模拟业务端）
        # -------------------------------------------------------------- #
        Write-Host "`n[3/5] 业务动作注入与建案/合案" -ForegroundColor Cyan
        $stamp = Get-Date -Format "yyyyMMddHHmmss"
        $verifyUser = "UV$stamp"
        $inject = & $venvPython -c @"
import json
from datetime import timedelta
from app.core.timeutil import utcnow
from app.db.session import SessionLocal
from app.simulator.actors import Actor, Address, Device, Network
from app.simulator.service import BusinessSimulator

user_id = "$verifyUser"
# 主体画像刻意选"老账号 + 干净指纹 + 代理 IP"：
#   * 老账号（60 天前注册）-> 避开"新账号高额订单"类规则，行为可归因到频次；
#   * 干净指纹 -> device_env_risk 为 0，避免一上来就被环境规则直接 Reject
#     （被 Reject 的订单不会落 biz_order，也就没有"可取消的订单"可验证联动）；
#   * 代理 IP -> 命中 RC_ENV_002（+20 分），与频次/金额规则叠加到 Review 区间。
actor = Actor(
    user_id=user_id,
    phone="13900009999",
    device=Device(device_id="DEV-VERIFY-P1", fingerprint={"os": "android", "is_emulator": False, "is_rooted": False}),
    network=Network(ip="203.0.113.77", ip_region="上海市", is_proxy=True),
    address=Address(address_hash="addr-verify-p1", region="上海市"),
)

with SessionLocal() as db:
    sim = BusinessSimulator(db, source="simulation")
    sim.register(actor, occurred_at=utcnow() - timedelta(days=60))
    results = []
    for index in range(5):
        # 5 笔 1200 元订单：累计 6000 触发"24h 金额 >= 5000"，第 5 笔同时触发
        # "1h 下单 >= 5 次"，叠加代理 IP 后进入 Review（>=60）并建案。
        outcome = sim.create_order(actor, amount=1200.0, quantity=1)
        results.append({
            "action": outcome.action,
            "allowed": outcome.allowed,
            "biz_no": outcome.biz_no,
            "case_no": outcome.decision.get("case_no"),
            "risk_score": outcome.decision.get("risk_score"),
            "hit_rules": [hit.get("rule_code") for hit in (outcome.decision.get("rule_hits") or [])],
        })
print("INJECT:" + json.dumps({"user_id": user_id, "results": results}, ensure_ascii=False))
"@
        $injectLine = ($inject | Select-String -Pattern "^INJECT:" | Select-Object -Last 1)
        if (-not $injectLine) {
            Assert-That "业务动作注入成功" $false (($inject | Select-Object -Last 5) -join " / ")
            throw "注入失败"
        }
        $payload = ($injectLine.ToString().Substring(7) | ConvertFrom-Json)
        $caseNos = @($payload.results | ForEach-Object { $_.case_no } | Where-Object { $_ })
        $uniqueCases = @($caseNos | Select-Object -Unique)
        $lastResult = $payload.results[-1]

        Assert-That "第 5 笔订单进入人工审核区间" ($lastResult.action -in @("Review", "Reject")) ("action=" + $lastResult.action + " score=" + $lastResult.risk_score)
        Assert-That "决策生成案件编号（case_no 非空）" ($null -ne $lastResult.case_no) ("case_no=" + $lastResult.case_no)
        Assert-That "窗口内多次触发合并为同一个案件" ($uniqueCases.Count -eq 1) ("案件数=" + $uniqueCases.Count)
        Assert-That "业务端按放行结果落了订单" (($payload.results | Where-Object { $_.allowed }).Count -gt 0) ""

        $caseNo = $uniqueCases[0]
        Write-Host ("         案件号：" + $caseNo + "｜用户：" + $payload.user_id) -ForegroundColor DarkGray

        # -------------------------------------------------------------- #
        # 4. 运营侧接口闭环
        # -------------------------------------------------------------- #
        Write-Host "`n[4/5] 案件接口闭环（登录 → 列表 → 详情 → 接手 → 处置 → 归档）" -ForegroundColor Cyan

        $login = Invoke-Json -Method Post -Path "/api/v1/auth/login" -Body @{ username = "auditor"; password = "audit123" }
        Assert-That "审核员登录成功" ($login.StatusCode -eq 200) ("HTTP " + $login.StatusCode + " " + $login.Error)
        $auditorToken = $login.Body.data.access_token
        $auditorHeaders = @{ Authorization = "Bearer $auditorToken" }

        $list = Invoke-Json -Method Get -Path ("/api/v1/cases?keyword=" + $payload.user_id) -Headers $auditorHeaders
        Assert-That "案件列表可查到新案件" (($list.StatusCode -eq 200) -and ($list.Body.data.total -ge 1)) ("HTTP " + $list.StatusCode)
        Assert-That "列表返回状态计数（工作台筛选标签）" ($null -ne $list.Body.data.status_counts) ""

        $detail = Invoke-Json -Method Get -Path ("/api/v1/cases/" + $caseNo) -Headers $auditorHeaders
        $d = $detail.Body.data
        Assert-That "案件详情返回事件时间线" ($d.events.Count -ge 1) ("events=" + $d.events.Count)
        Assert-That "案件详情返回完整证据链（命中规则）" ($d.focus.hit_rules.Count -ge 1) ("hit_rules=" + $d.focus.hit_rules.Count)
        Assert-That "案件详情返回特征分组" ($d.focus.features.Count -ge 1) ("groups=" + $d.focus.features.Count)
        Assert-That "案件详情返回用户画像" ($null -ne $d.profile.customer) ""
        Assert-That "案件详情返回关联实体图谱" ($d.graph.nodes.Count -ge 1) ("nodes=" + $d.graph.nodes.Count)
        Assert-That "案件详情返回当前业务单据" ($null -ne $d.biz_doc) ("kind=" + $d.biz_doc.kind)

        $claim = Invoke-Json -Method Post -Path ("/api/v1/cases/" + $caseNo + "/claim") -Headers $auditorHeaders -Body @{}
        Assert-That "接手案件成功（pending → processing）" (($claim.StatusCode -eq 200) -and ($claim.Body.data.status -eq "processing")) ("HTTP " + $claim.StatusCode + " status=" + $claim.Body.data.status)
        Assert-That "接手人记录为当前账号" ($claim.Body.data.handler -eq "auditor") ("handler=" + $claim.Body.data.handler)

        $claimAgain = Invoke-Json -Method Post -Path ("/api/v1/cases/" + $caseNo + "/claim") -Headers $auditorHeaders -Body @{}
        Assert-That "重复接手返回 409（并发冲突可见）" ($claimAgain.StatusCode -eq 409) ("HTTP " + $claimAgain.StatusCode)

        $dispose = Invoke-Json -Method Post -Path ("/api/v1/cases/" + $caseNo + "/dispose") -Headers $auditorHeaders -Body @{
            business_result = "reject"
            risk_actions    = @("block_order", "blacklist_user")
            remark          = "P1 验收脚本：确认高频下单套利，拦截订单并拉黑账号"
        }
        Assert-That "处置提交成功" ($dispose.StatusCode -eq 200) ("HTTP " + $dispose.StatusCode + " " + $dispose.Body.message)
        Assert-That "处置后案件状态为 disposed" ($dispose.Body.data.case.status -eq "disposed") ("status=" + $dispose.Body.data.case.status)
        $itemResults = @($dispose.Body.data.items | ForEach-Object { $_.risk_action + "=" + $_.exec_result })
        Assert-That "处置未出现联动失败（failed）" (-not ($dispose.Body.data.items | Where-Object { $_.exec_result -eq "failed" })) ($itemResults -join ", ")
        Assert-That "订单拦截动作执行成功" (($dispose.Body.data.items | Where-Object { $_.risk_action -eq "block_order" }).exec_result -eq "success") ($itemResults -join ", ")
        Write-Host ("         联动结果：" + ($itemResults -join "｜")) -ForegroundColor DarkGray

        # 归档是 admin 专属：这里同时验证"审核员被拒 + 管理员成功"这条权限边界，
        # 而不是只测 happy path —— 权限矩阵写错时，只有这种断言能发现。
        $archiveDenied = Invoke-Json -Method Post -Path ("/api/v1/cases/" + $caseNo + "/archive") -Headers $auditorHeaders
        Assert-That "审核员归档被拒（403）" ($archiveDenied.StatusCode -eq 403) ("HTTP " + $archiveDenied.StatusCode)

        $adminLogin = Invoke-Json -Method Post -Path "/api/v1/auth/login" -Body @{ username = "admin"; password = "admin123" }
        Assert-That "管理员登录成功" ($adminLogin.StatusCode -eq 200) ("HTTP " + $adminLogin.StatusCode + " " + $adminLogin.Error)
        $adminHeaders = @{ Authorization = "Bearer " + $adminLogin.Body.data.access_token }
        $archive = Invoke-Json -Method Post -Path ("/api/v1/cases/" + $caseNo + "/archive") -Headers $adminHeaders
        Assert-That "管理员归档成功（disposed → archived）" (($archive.StatusCode -eq 200) -and ($archive.Body.data.case.status -eq "archived")) ("HTTP " + $archive.StatusCode + " status=" + $archive.Body.data.case.status)
    }

    # ------------------------------------------------------------------ #
    # 5. 落库与审计链
    # ------------------------------------------------------------------ #
    Write-Host "`n[5/5] 落库结果与审计哈希链" -ForegroundColor Cyan
    $final = & $venvPython -c @"
import json
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from app.core.config import settings
from app.models.audit import RcAuditLog
from app.models.biz import BizCustomer, BizOrder
from app.models.case import RcCase, RcCaseAction, RcCaseActionItem, RcCaseEvent
from app.models.rclist import LIST_BLACK, RcListEntry
from app.services import audit_service

engine = create_engine(settings.db_url, pool_pre_ping=True)
with Session(engine) as db:
    counts = {
        "case": db.execute(select(func.count()).select_from(RcCase)).scalar_one(),
        "case_event": db.execute(select(func.count()).select_from(RcCaseEvent)).scalar_one(),
        "case_action": db.execute(select(func.count()).select_from(RcCaseAction)).scalar_one(),
        "action_item": db.execute(select(func.count()).select_from(RcCaseActionItem)).scalar_one(),
    }
    statuses = dict(db.execute(select(RcCase.status, func.count()).group_by(RcCase.status)).all())
    audited = dict(
        db.execute(
            select(RcAuditLog.action, func.count())
            .where(RcAuditLog.action.in_(["case_create", "case_claim", "case_dispose", "case_archive"]))
            .group_by(RcAuditLog.action)
        ).all()
    )
    # 处置联动是否真的落到业务表：被取消的订单 + 黑名单条目 + 置黑的业务用户
    cancelled = db.execute(
        select(func.count()).select_from(BizOrder).where(BizOrder.status == "cancelled")
    ).scalar_one()
    blacklist = db.execute(
        select(func.count()).select_from(RcListEntry).where(RcListEntry.list_type == LIST_BLACK)
    ).scalar_one()
    blacklisted_users = db.execute(
        select(func.count()).select_from(BizCustomer).where(BizCustomer.status == "blacklisted")
    ).scalar_one()
    # 决策行是否真的写回了 case_no（"决策 → 案件"这条接缝的证据）
    decisions_with_case = db.execute(
        text("SELECT COUNT(*) FROM rc_decision WHERE case_no IS NOT NULL")
    ).scalar()
    decisions_total = db.execute(text("SELECT COUNT(*) FROM rc_decision")).scalar()
    chain = audit_service.verify(db).to_dict()
print("FINAL:" + json.dumps({
    "counts": counts, "statuses": {str(k): int(v) for k, v in statuses.items()},
    "audited": {str(k): int(v) for k, v in audited.items()},
    "cancelled_orders": int(cancelled), "blacklist_entries": int(blacklist),
    "blacklisted_customers": int(blacklisted_users),
    "decisions_with_case": int(decisions_with_case), "decisions_total": int(decisions_total),
    "chain": chain,
}, ensure_ascii=False))
"@
    $finalLine = ($final | Select-String -Pattern "^FINAL:" | Select-Object -Last 1)
    if (-not $finalLine) {
        Assert-That "落库统计读取成功" $false (($final | Select-Object -Last 5) -join " / ")
    }
    else {
        $f = ($finalLine.ToString().Substring(6) | ConvertFrom-Json)
        Write-Host ("         案件 " + $f.counts.case + "｜关联事件 " + $f.counts.case_event + "｜处置 " + $f.counts.case_action + "｜动作明细 " + $f.counts.action_item) -ForegroundColor DarkGray
        Write-Host ("         决策带案件号 " + $f.decisions_with_case + " / " + $f.decisions_total + "｜已取消订单 " + $f.cancelled_orders + "｜黑名单 " + $f.blacklist_entries) -ForegroundColor DarkGray
        Assert-That "案件-事件关联已落库" ($f.counts.case_event -ge 1) ""
        Assert-That "决策行写回了案件号（决策→案件接缝）" ($f.decisions_with_case -ge 1) ("case_no 非空决策 " + $f.decisions_with_case)
        Assert-That "审计含建案/接手/处置记录" (($f.audited.case_create -ge 1) -and ($f.audited.case_claim -ge 1) -and ($f.audited.case_dispose -ge 1)) (($f.audited | ConvertTo-Json -Compress))
        if (-not $SkipHttp) {
            Assert-That "处置联动：订单被取消" ($f.cancelled_orders -ge 1) ("cancelled=" + $f.cancelled_orders)
            Assert-That "处置联动：黑名单写入" ($f.blacklist_entries -ge 1) ("entries=" + $f.blacklist_entries)
            Assert-That "处置联动：业务用户置黑" ($f.blacklisted_customers -ge 1) ("blacklisted=" + $f.blacklisted_customers)
            Assert-That "审计含归档记录" ($f.audited.case_archive -ge 1) ""
        }
        Assert-That "审计哈希链自校验通过" ($f.chain.valid -eq $true) ("total=" + $f.chain.total + " first_broken=" + $f.chain.first_broken_id)
    }

    # ------------------------------------------------------------------ #
    # 附加：单元与集成测试
    # ------------------------------------------------------------------ #
    Write-Host "`n[附加] 单元与集成测试（pytest）" -ForegroundColor Cyan
    $pytestOut = & $venvPython -m pytest -q 2>&1
    Assert-That "pytest 全绿" ($LASTEXITCODE -eq 0) ($pytestOut | Select-String -Pattern "failed|error" | Select-Object -Last 3)
    Write-Host ("         " + (($pytestOut | Select-String -Pattern "\d+ passed" | Select-Object -Last 1))) -ForegroundColor DarkGray
}
finally {
    Pop-Location
}

Write-Host ""
if ($script:failed -eq 0) {
    Write-Host ("==> 验收结果：通过 " + $script:passed + " 项，失败 0 项") -ForegroundColor Green
    exit 0
}
Write-Host ("==> 验收结果：通过 " + $script:passed + " 项，失败 " + $script:failed + " 项") -ForegroundColor Red
exit 1
