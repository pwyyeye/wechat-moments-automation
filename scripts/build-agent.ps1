param(
    [string]$Python = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = (Resolve-Path (Join-Path $repoRoot $Python)).Path
$dotnetCommand = Get-Command dotnet -ErrorAction SilentlyContinue
if ($dotnetCommand) {
    $dotnetPath = $dotnetCommand.Source
} else {
    $dotnetPath = Join-Path $env:ProgramFiles "dotnet\dotnet.exe"
    if (-not (Test-Path -LiteralPath $dotnetPath)) {
        throw ".NET 8+ SDK is required to build the bundled WeChat UIA helper."
    }
}

Push-Location $repoRoot
try {
    & $pythonPath -m pip install -e ".[full,agent,packaging]"
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE." }
    & $pythonPath -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Test suite failed with exit code $LASTEXITCODE." }

    # Some integration tests start the repository-local UIA monitor. Stop only
    # that exact helper before replacing its self-contained build output.
    $uiaPublishPath = Join-Path $repoRoot "src\cs_uia_service\publish\WeChatUIA.exe"
    Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -eq $uiaPublishPath } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

    & $dotnetPath publish "src\cs_uia_service\WeChatUIA.csproj" -c Release -r win-x64 --self-contained true -o "src\cs_uia_service\publish"
    if ($LASTEXITCODE -ne 0) { throw "UIA helper build failed with exit code $LASTEXITCODE." }
    & $pythonPath -m PyInstaller --noconfirm --clean "packaging\WechatPublisherAgent.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE." }

    $compilerCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    $compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($compiler) {
        & $compiler "packaging\installer.iss"
        if ($LASTEXITCODE -ne 0) { throw "Installer compilation failed with exit code $LASTEXITCODE." }
    } else {
        Write-Warning "Inno Setup 6 was not found; the onedir build is ready but the setup EXE was not compiled."
    }
} finally {
    Pop-Location
}
