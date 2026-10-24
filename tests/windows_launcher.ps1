# Native Windows smoke test of the launcher and its real staged updater, with no network or Docker.
$ErrorActionPreference = "Stop"
$repository = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $repository "tools\launch_windows.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$errors)
if ($errors.Count) {
    throw ("Invalid PowerShell syntax: " + ($errors | ForEach-Object { $_.Message } | Out-String))
}
# Parse the initial bootstrap too; the update smoke only exercises the launcher.
$installErrors = $null
$installTokens = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repository "install.ps1"), [ref]$installTokens, [ref]$installErrors
)
if ($installErrors.Count) {
    throw ("Invalid installer script: " + ($installErrors | ForEach-Object { $_.Message } | Out-String))
}
foreach ($function in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    . ([scriptblock]::Create($function.Extent.Text))
}

# A working interpreter wins over the stale py.exe registry mapping.
$python = Find-WorkingPython
if (-not $python) { throw "Windows CI runner had no working Python to exercise the launcher." }
$prefix = $python.Prefix
& $python.File @prefix -c "import sys; assert sys.version_info >= (3, 10)"
if ($LASTEXITCODE -ne 0) { throw "Detected Python interpreter did not execute." }

$testing = Join-Path ([IO.Path]::GetTempPath()) ("trading-launch-test-" + [guid]::NewGuid().ToString("N"))
$Root = Join-Path $testing "TradingLab"
$VersionFile = Join-Path $Root ".tradinglab-version"
$sample = Join-Path $testing "sample\trading-revision"
$fixture = Join-Path $testing "mock-source.zip"
New-Item -ItemType Directory -Force -Path (Join-Path $Root "tools"), (Join-Path $Root "results"), (Join-Path $Root "secrets"),
    (Join-Path $sample "tools"), (Join-Path $sample "secrets") | Out-Null
try {
    Set-Content -LiteralPath (Join-Path $Root ".env") -Value "TWS_USERID=private"
    Set-Content -LiteralPath (Join-Path $Root "lab.toml") -Value "private config"
    Set-Content -LiteralPath (Join-Path $Root "results\orders.txt") -Value "keep history"
    Set-Content -LiteralPath (Join-Path $Root "secrets\tws_password") -Value "keep password"
    Set-Content -LiteralPath (Join-Path $Root "tools\start_app.py") -Value "old program"
    Set-Content -LiteralPath (Join-Path $sample "tools\start_app.py") -Value "new program"
    Set-Content -LiteralPath (Join-Path $sample "tools\launch_windows.ps1") -Value "# new launcher"
    Set-Content -LiteralPath (Join-Path $sample "compose.app.yml") -Value "services: {}"
    Set-Content -LiteralPath (Join-Path $sample "lab.toml") -Value "malicious overwrite"
    Set-Content -LiteralPath (Join-Path $sample "secrets\tws_password") -Value "malicious overwrite"
    Compress-Archive -LiteralPath $sample -DestinationPath $fixture

    # No HTTP request: locally provision the GitHub-shaped archive.
    function Invoke-WebRequest {
        param($Uri, $OutFile, [switch]$UseBasicParsing)
        Copy-Item -LiteralPath $fixture -Destination $OutFile
    }
    $sha = "a" * 40
    Install-Version $sha

    if ((Get-Content (Join-Path $Root "tools\start_app.py") -Raw).Trim() -ne "new program") {
        throw "Downloaded code was not installed."
    }
    if ((Get-Content $VersionFile -Raw).Trim() -ne $sha) { throw "Version was not recorded." }
    foreach ($item in @(
        @(".env", "TWS_USERID=private"), @("lab.toml", "private config"),
        @("results\orders.txt", "keep history"), @("secrets\tws_password", "keep password")
    )) {
        if ((Get-Content (Join-Path $Root $item[0]) -Raw).Trim() -ne $item[1]) {
            throw "Updater overwrote private state: $($item[0])"
        }
    }
    Write-Host "Windows launcher parse, Python probe and staged update/preservation smoke passed."
} finally {
    Remove-Item -LiteralPath $testing -Recurse -Force -ErrorAction SilentlyContinue
}
