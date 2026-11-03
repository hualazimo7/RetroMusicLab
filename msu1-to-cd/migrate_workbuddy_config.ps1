# -*- coding: utf-8 -*-
# 把 WorkBuddy 配置目录（默认 C:\Users\<你>\.workbuddy-ai）迁到 D 盘，
# 原位置留一个目录 junction，WorkBuddy 完全无感。
#
# ⚠ 运行前**必须完全退出 WorkBuddy**（托盘图标也要退），否则文件被占用会失败。
#
# 用法：右键「使用 PowerShell 运行」，或在 PowerShell 里执行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\migrate_workbuddy_config.ps1

$ErrorActionPreference = "Stop"

$src = Join-Path $env:USERPROFILE ".workbuddy-ai"
$dst = "D:\workbuddy-ai"

Write-Host "=== WorkBuddy 配置迁移 ===" -ForegroundColor Cyan
Write-Host "源: $src"
Write-Host "目标: $dst"
Write-Host ""

# ---- 1. 前置检查 ----
if (-not (Test-Path $src)) {
    Write-Host "[X] 源目录不存在：$src" -ForegroundColor Red
    exit 1
}
$item = Get-Item $src -Force
if ($item.LinkType) {
    Write-Host "[!] 源目录已经是一个链接（$($item.LinkType) -> $($item.Target)），无需再迁。" -ForegroundColor Yellow
    exit 0
}

$running = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -match 'WorkBuddy|CodeBuddy' }
if ($running) {
    Write-Host "[X] 检测到 WorkBuddy 还在运行，请先完全退出（含托盘图标）：" -ForegroundColor Red
    $running | Select-Object Id, ProcessName | Format-Table -AutoSize
    exit 1
}
Write-Host "[OK] 没有 WorkBuddy 进程在跑" -ForegroundColor Green

if (-not (Test-Path "D:\")) {
    Write-Host "[X] 没有 D 盘，无法迁移。" -ForegroundColor Red
    exit 1
}
if (Test-Path $dst) {
    Write-Host "[X] 目标已存在：$dst" -ForegroundColor Red
    Write-Host "    如果是上次迁移的残留，请先确认里面数据完整，或改名后重跑。" -ForegroundColor Yellow
    exit 1
}

$sizeGB = [math]::Round(
    (Get-ChildItem $src -Recurse -Force -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum / 1GB, 2)
Write-Host "[i] 待迁移大小: $sizeGB GB（复制 + 校验，请耐心等）" -ForegroundColor Cyan
Write-Host ""
$ans = Read-Host "确认开始？输入 Y 继续"
if ($ans -ne "Y" -and $ans -ne "y") {
    Write-Host "已取消。" -ForegroundColor Yellow
    exit 0
}

# ---- 2. 复制（不用 /MOVE，先复制再校验，最后才删源）----
Write-Host ""
Write-Host "[1/4] 复制中（robocopy）…" -ForegroundColor Cyan
robocopy $src $dst /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
$rc = $LASTEXITCODE
# robocopy: 0-7 都是成功（0=无变化,1=有复制,2=有额外项,3=1+2,...）
if ($rc -ge 8) {
    Write-Host "[X] 复制失败（robocopy rc=$rc）。源目录未动，可直接重跑。" -ForegroundColor Red
    exit 1
}
Write-Host "      完成（rc=$rc）" -ForegroundColor Green

# ---- 3. 校验：文件数 + 总字节 ----
Write-Host "[2/4] 校验文件数与体积…" -ForegroundColor Cyan
function Get-Stats($p) {
    $f = Get-ChildItem $p -Recurse -Force -File -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        N = $f.Count
        B = ($f | Measure-Object -Property Length -Sum).Sum
    }
}
$a = Get-Stats $src
$b = Get-Stats $dst
Write-Host ("      源: {0} 个文件 / {1:N2} GB" -f $a.N, ($a.B/1GB))
Write-Host ("      新: {0} 个文件 / {1:N2} GB" -f $b.N, ($b.B/1GB))
if ($a.N -ne $b.N -or $a.B -ne $b.B) {
    Write-Host "[X] 校验不一致，已停止。源目录**没有**被删除，可放心重跑。" -ForegroundColor Red
    exit 1
}
Write-Host "      一致" -ForegroundColor Green

# ---- 4. 删源 + 建 junction ----
Write-Host "[3/4] 删除 C 盘原目录…" -ForegroundColor Cyan
Remove-Item $src -Recurse -Force
if (Test-Path $src) {
    Write-Host "[X] 删除失败，目标数据在 $dst，源仍在。请手动处理。" -ForegroundColor Red
    exit 1
}

Write-Host "[4/4] 建立 junction…" -ForegroundColor Cyan
cmd /c mklink /J "$src" "$dst" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] mklink 失败。数据在 $dst，请手动执行：" -ForegroundColor Red
    Write-Host "    mklink /J `"$src`" `"$dst`"" -ForegroundColor Yellow
    exit 1
}

# ---- 5. 结果 ----
Write-Host ""
$chk = Get-Item $src -Force
Write-Host "=== 完成 ===" -ForegroundColor Green
Write-Host ("  {0}" -f $chk.FullName)
Write-Host ("  类型: {0}  ->  {1}" -f $chk.LinkType, ($chk.Target -join ', '))
Write-Host ""
Write-Host "现在可以正常启动 WorkBuddy 了，它会照旧读写 C 盘那个路径（实际落在 D 盘）。"
Write-Host "想撤销：退出 WorkBuddy -> 删掉 junction -> 把 $dst 移回来。"
