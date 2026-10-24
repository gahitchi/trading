# Trading Lab: one-command install and update for Windows 10/11 (paper trading only).
#
#   irm https://raw.githubusercontent.com/gahitchi/trading/main/install.ps1 | iex
#
# Installs what is missing (Docker Desktop, Python), downloads Trading Lab into %LOCALAPPDATA%\TradingLab,
# asks for your IBKR paper login once, starts it, and puts a Trading Lab icon on the desktop and in the Start
# menu. Running it again updates the program and keeps your settings, credentials and history.
# It never places an order: trading starts only when you press Start in the app.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = "gahitchi/trading"
$Home_ = Join-Path $env:LOCALAPPDATA "TradingLab"
$Private = @(".env", "secrets", "results", "data", "research", "lab.toml")  # never overwritten by an update

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Have($command) { [bool](Get-Command $command -ErrorAction SilentlyContinue) }

function Install-Package($id, $name) {
    if (-not (Have "winget")) {
        throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again."
    }
    Step "Installing $name (Windows may ask for permission)"
    winget install --exact --id $id --silent --accept-package-agreements --accept-source-agreements
    # Make the new programs visible to this window without reopening it.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

# 1. Docker Desktop (it brings WSL 2 with it; nothing else to install by hand)
if (-not (Have "docker")) {
    Install-Package "Docker.DockerDesktop" "Docker Desktop"
    $docker = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"
    if (Test-Path $docker) { $env:Path += ";$docker" }
}
if (-not (Have "docker")) {
    throw "Docker Desktop was installed but is not usable yet. Restart Windows, then run the same command again."
}

# 2. The downloaded launcher validates an executable Python interpreter.
# A stale py.exe registration may point to a missing file from another account.

# 3. Download (or update) Trading Lab, keeping private files
Step "Downloading Trading Lab into $Home_"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("tradinglab-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $revision = [string](Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/commits/main" -Headers @{ "User-Agent" = "TradingLab-Windows-Installer" }).sha
    if ($revision -notmatch '^[0-9a-f]{40}$') { throw "Invalid version received from GitHub." }
    $zip = Join-Path $tmp "source.zip"
    Invoke-WebRequest "https://github.com/$Repo/archive/$revision.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $tmp
    $src = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like "trading-*" } | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $Home_ | Out-Null
    foreach ($item in Get-ChildItem $src.FullName -Force) {
        $dest = Join-Path $Home_ $item.Name
        if ($Private -contains $item.Name -and (Test-Path $dest)) { continue }
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        Move-Item $item.FullName $dest
    }
    Set-Content -LiteralPath (Join-Path $Home_ ".tradinglab-version") -Encoding ASCII -NoNewline -Value $revision
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# 4. Start it: login questions on first run, then the app window and the icons
Step "Starting Trading Lab (the first start builds it: allow 5 to 15 minutes)"
Set-Location $Home_
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Home_ "tools\launch_windows.ps1") -SkipUpdate
if ($LASTEXITCODE -ne 0) { throw "Trading Lab did not start; the message above says why." }
Write-Host "`nDone. Next time, open Trading Lab from its desktop or Start menu icon." -ForegroundColor Green
