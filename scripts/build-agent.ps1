param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = (Resolve-Path (Join-Path $repoRoot $Python)).Path

Push-Location $repoRoot
try {
    & $pythonPath -m pip install -e ".[full,agent,packaging]"
    & $pythonPath -m pytest -q
    & $pythonPath -m PyInstaller --noconfirm --clean "packaging\WechatPublisherAgent.spec"

    $compilerCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    $compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($compiler) {
        & $compiler "packaging\installer.iss"
    } else {
        Write-Warning "Inno Setup 6 was not found; the onedir build is ready but the setup EXE was not compiled."
    }
} finally {
    Pop-Location
}
