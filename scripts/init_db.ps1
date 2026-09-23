#Requires -Version 7.0
<#
.SYNOPSIS
    初始化本地开发数据库：建库 → 迁移 → 种子。幂等，可反复执行。

.DESCRIPTION
    对应 docs/ARCHITECTURE.md §13 启动序列的第 2 步，把三件事一次做完：
      1. 建库    app.db.bootstrap      —— CREATE DATABASE IF NOT EXISTS
      2. 迁移    alembic upgrade head  —— 18 张业务表 + 审计表只增触发器
      3. 种子    app.seeds             —— 3 账号 / 8 条配置 / 业务端 API Key

    幂等性说明：三步都是 IF NOT EXISTS 或 upsert 语义，重复执行不会报错、
    也不会产生重复数据（docs/PRD.md §17.1 要求）。

.PARAMETER Reset
    先 DROP 整个库再重建。属于破坏性操作，必须同时显式加 -Force，
    这是刻意的"双钥匙"设计：避免误敲一个参数就把本地数据清空。

.PARAMETER SkipSeed
    只建库与迁移，不写种子数据。用于只想验证 schema 变更的场景。

.EXAMPLE
    pwsh -File scripts/init_db.ps1
    常规初始化 / 重复执行。

.EXAMPLE
    pwsh -File scripts/init_db.ps1 -Reset -Force
    从零重建（迁移脚本改动后验证用）。
#>
[CmdletBinding()]
param(
    [switch]$Reset,
    [switch]$Force,
    [switch]$SkipSeed
)

$ErrorActionPreference = "Stop"

# 控制台按 UTF-8 输出：Python 侧按 UTF-8 打印中文，而 Windows 默认控制台码页是
# GBK(cp936)，不设置的话中文提示会变成乱码，看起来像"脚本出错"。
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"

Write-Host "==> 电商风险控制系统 / 数据库初始化" -ForegroundColor Cyan
Write-Host "    仓库根目录: $repoRoot"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "[失败] 未找到虚拟环境: $python" -ForegroundColor Red
    Write-Host "       请先创建并安装依赖：" -ForegroundColor Yellow
    Write-Host "         cd backend" -ForegroundColor Yellow
    Write-Host "         python -m venv .venv" -ForegroundColor Yellow
    Write-Host "         .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

if ($Reset -and -not $Force) {
    Write-Host "[中止] -Reset 会删除整个数据库，必须同时加 -Force 才执行。" -ForegroundColor Red
    Write-Host "       确认要继续请执行：" -ForegroundColor Yellow
    Write-Host "         pwsh -File scripts/init_db.ps1 -Reset -Force" -ForegroundColor Yellow
    exit 1
}

# 所有 alembic / 模块命令都必须在 backend/ 目录下执行：
# alembic.ini 里的 script_location 与 .env 的查找路径都相对于该目录。
Push-Location $backendDir
try {
    if ($Reset) {
        Write-Host "`n[1/4] 删除数据库（Reset）" -ForegroundColor Yellow
        & $python -c @"
from app.core.config import settings
from app.db.bootstrap import drop_database
drop_database(settings.DB_NAME)
"@
        if ($LASTEXITCODE -ne 0) { throw "删库失败（退出码 $LASTEXITCODE）" }
    }

    Write-Host "`n[2/4] 建库" -ForegroundColor Cyan
    & $python -m app.db.bootstrap
    if ($LASTEXITCODE -ne 0) { throw "建库失败（退出码 $LASTEXITCODE）" }

    Write-Host "`n[3/4] 执行迁移" -ForegroundColor Cyan
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "迁移失败（退出码 $LASTEXITCODE）" }

    if ($SkipSeed) {
        Write-Host "`n[4/4] 跳过种子数据（-SkipSeed）" -ForegroundColor Yellow
    }
    else {
        Write-Host "`n[4/4] 写入种子数据" -ForegroundColor Cyan
        & $python -m app.seeds
        if ($LASTEXITCODE -ne 0) { throw "种子写入失败（退出码 $LASTEXITCODE）" }
    }
}
catch {
    Write-Host "`n[失败] $_" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}

Write-Host "`n==> 完成。预置账号：admin/admin123、strategist/strategy123、auditor/audit123" -ForegroundColor Green

