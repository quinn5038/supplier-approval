param([switch]$Test, [switch]$Demo, [switch]$InstallOnly)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
Set-Location -LiteralPath $PSScriptRoot

function Test-Python([string]$Executable, [string[]]$PrefixArgs = @()) {
    if (-not $Executable) { return $false }
    try {
        & $Executable @PrefixArgs -I -c 'import encodings,sys;assert sys.version_info >= (3,11)' *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-BasePython {
    $bundled = Join-Path $PSScriptRoot 'runtime/python312/python.exe'
    if ((Test-Path -LiteralPath $bundled) -and (Test-Python $bundled)) {
        return @{ Command = $bundled; Args = @() }
    }
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python 'py' @('-3.12'))) {
        return @{ Command = 'py'; Args = @('-3.12') }
    }
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python 'py' @('-3.11'))) {
        return @{ Command = 'py'; Args = @('-3.11') }
    }
    # A normal per-user Python installation is not necessarily added to PATH.
    # Check it explicitly so a copied/moved virtual environment can repair itself.
    $localPythonRoot = Join-Path $env:LOCALAPPDATA 'Programs/Python'
    if (Test-Path -LiteralPath $localPythonRoot) {
        $localPythons = Get-ChildItem -LiteralPath $localPythonRoot -Directory -Filter 'Python*' |
            Sort-Object Name -Descending
        foreach ($directory in $localPythons) {
            $candidate = Join-Path $directory.FullName 'python.exe'
            if ((Test-Path -LiteralPath $candidate) -and (Test-Python $candidate)) {
                return @{ Command = $candidate; Args = @() }
            }
        }
    }
    $systemPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($systemPython -and (Test-Python $systemPython)) {
        return @{ Command = $systemPython; Args = @() }
    }
    throw '未找到可用的 Python 3.11/3.12。请先安装 Python，或重新运行项目安装程序。'
}

$projectPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$venvHealthy = (Test-Path -LiteralPath $projectPython) -and (Test-Python $projectPython)
if ($venvHealthy) {
    & $projectPython -I -c 'import pip' *> $null
    $venvHealthy = $LASTEXITCODE -eq 0
}
if (-not $venvHealthy) {
    if (Test-Path -LiteralPath '.venv') {
        $backup = '.venv-broken-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Write-Host "现有 Python 环境损坏，备份到 $backup"
        Move-Item -LiteralPath '.venv' -Destination $backup
    }
    $base = Find-BasePython
    Write-Host "正在使用 $($base.Command) 重建 Python 环境..."
    $baseArgs = @($base.Args)
    & $base.Command @baseArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败' }
}

$dependencyFile = if ($Test) { 'requirements-dev.txt' } else { 'requirements.txt' }
& $projectPython -m pip install -r $dependencyFile
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
& $projectPython -I -c 'import encodings,fastapi,jinja2,requests,uvicorn,yaml'
if ($LASTEXITCODE -ne 0) { throw '环境校验失败：WebUI 依赖仍不可用' }
if ($InstallOnly) { exit 0 }
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
