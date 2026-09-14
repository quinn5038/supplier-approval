param([switch]$Test, [switch]$Demo)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 3.12 环境失败' }
}
$projectPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$dependencyFile = if ($Test) { 'requirements-dev.txt' } else { 'requirements.txt' }
& $projectPython -m pip install -r $dependencyFile
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
if ($Test) {
    & $projectPython -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw '静态检查失败' }
    & $projectPython -m pytest
    if ($LASTEXITCODE -ne 0) { throw '回归测试失败' }
    exit 0
}
$env:DRY_RUN = 'true'
$env:ENABLE_LIVE_APPROVAL = 'false'
if ($Demo) { $env:DEMO_MODE = 'true' }
& $projectPython launch_web.py
