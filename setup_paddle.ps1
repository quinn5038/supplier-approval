$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$mainPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$ocrPython = Join-Path $PSScriptRoot '.paddle-venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $mainPython)) { throw '请先安装主项目环境' }
if (-not (Test-Path -LiteralPath $ocrPython)) {
    & $mainPython -m venv .paddle-venv
    if ($LASTEXITCODE -ne 0) { throw '创建 OCR 环境失败' }
}
& $ocrPython -m pip install paddlepaddle -r offline_desens/requirements.txt -c offline_desens/constraints-cpu.txt
if ($LASTEXITCODE -ne 0) { throw '安装 OCR 依赖失败' }
& $mainPython offline_desens/download_models.py
if ($LASTEXITCODE -ne 0) { throw '模型准备失败' }
& $ocrPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'OCR 依赖检查失败' }
& $ocrPython offline_desens/smoke_test.py
if ($LASTEXITCODE -ne 0) { throw 'OCR 实际识别检查失败' }
Write-Host '本地 OCR 配置完成。重新启动 Web 服务后查看启动检查。'
