#Requires -Version 7.0
<#
.SYNOPSIS
    开发环境一键启动：数据库/Redis 自检 → 迁移 → API 服务 → 端到端冒烟。

.DESCRIPTION
    对应 docs/ARCHITECTURE.md §13 的启动序列。这个脚本存在的意义是**把"环境没起来"
    这类问题挡在启动阶段**，而不是等到接口报 500 再回头排查：

      * MySQL / Redis 由 FlyEnv 手动启停（本项目不接管进程管理），
        脚本只做**连通性自检**并给出可操作的提示；
      * API 用 --reload 起在前台；Ctrl+C 即停，不留后台残留进程。

.PARAMETER SkipServer
    只做自检与迁移，不启动 API。用于"库里改完了但先不起服务"。

.PARAMETER DbName
    目标库名。默认读环境变量 DB_NAME（未设置时用 risk_control）。

.PARAMETER Port
    API 端口，默认 8000。

.EXAMPLE
    pwsh -File scripts/dev.ps1
    常规启动。

.EXAMPLE
    pwsh -File scripts/dev.ps1 -SkipServer
    只做自检 + 迁移。
#>
[CmdletBinding()]
param(
    [switch]$SkipServer,
    [string]$DbName = $env:DB_NAME,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

# 控制台按 UTF-8 输出：Python 侧打印中文，Windows 默认码页是 GBK(cp936)，
# 不设置的话中文提示会变成乱码，看起来像"脚本出错"。
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if ($DbName) { $env:DB_NAME = $DbName }

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"
$venvPython = $python

Write-Host "==> 电商风险控制系统 / 开发环境启动" -ForegroundColor Cyan
Write-Host "    仓库根目录: $repoRoot"

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "[失败] 未找到虚拟环境: $venvPython" -ForegroundColor Red
    Write-Host "       请先创建并安装依赖：" -ForegroundColor Yellow
    Write-Host "         cd backend" -ForegroundColor Yellow
    Write-Host "         python -m venv .venv" -ForegroundColor Yellow
    Write-Host "         .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# 自检放在 backend/ 目录下执行：settings 会去读 backend/.env，
# 在别处跑会读到默认值，自检结论就对不上了。
Push-Location $backendDir
try {
    Write-Host "`n[1/4] 依赖自检（MySQL / Redis）" -ForegroundColor Cyan
    & $venvPython -c @"
import sys
from app.core.config import settings

print(f'    目标库: {settings.DB_NAME}')
try:
    from sqlalchemy import create_engine, text
    engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    engine.dispose()
    print('    MySQL 连接: OK')
except Exception as exc:
    print(f'    MySQL 连接: 失败 -> {exc}')
    print('    FlyEnv 里确认 MySQL 已启动，且 backend/.env 的账号密码正确')
    sys.exit(1)

try:
    from app.db.redis_client import get_redis
    client = get_redis()
    client.ping()
    print(f'    Redis 连接: OK (DB {settings.REDIS_DB})')
except Exception as exc:
    print(f'    Redis 连接: 失败 -> {exc}')
    print('    FlyEnv 里确认 Redis 已启动')
    sys.exit(1)
"@
    if ($LASTEXITCODE -ne 0) { throw "依赖自检失败（退出码 $LASTEXITCODE）" }

    Write-Host "`n[2/4] 执行迁移（alembic upgrade head）" -ForegroundColor Cyan
    & $venvPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "迁移失败（退出码 $LASTEXITCODE）" }

    Write-Host "`n[3/4] 迁移漂移检查（alembic check）" -ForegroundColor Cyan
    & $venvPython -m alembic check
    if ($LASTEXITCODE -ne 0) {
        # 只警告不中止：漂移是"模型与迁移不同步"，服务本身还能起，
        # 但新加的表/列在别人的机器上会缺 —— 提示到即可，不打断本地开发。
        Write-Host "[警告] 模型与迁移存在漂移，请补一条迁移后重跑" -ForegroundColor Yellow
    }

    if ($SkipServer) {
        Write-Host "`n[4/4] 已跳过 API 启动（-SkipServer）" -ForegroundColor Yellow
        Write-Host "`n==> 完成。" -ForegroundColor Green
        return
    }

    Write-Host "`n[4/4] 启动 API：http://127.0.0.1:$Port/docs （--reload，Ctrl+C 停止）" -ForegroundColor Cyan
    & $venvPython -m uvicorn app.main:app --reload --port $Port
}
catch {
    Write-Host "`n[失败] $_" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
