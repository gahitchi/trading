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

# Existing installs with the version-aware launcher use its staged updater.
# Do not destructively replace a running Docker application.
$installedLauncher = Join-Path $Home_ "tools\launch_windows.ps1"
if (Test-Path -LiteralPath $installedLauncher) {
    Step "Checking installed Trading Lab version"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installedLauncher -UpdateOnly
    if ($LASTEXITCODE -ne 0) { throw "Update failed; the previous installed version was preserved." }
    Step "Opening Trading Lab"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installedLauncher -SkipUpdate
    if ($LASTEXITCODE -ne 0) { throw "Trading Lab did not start; review the launcher error above." }
    return
}
# Upgrading an older installation must not overwrite code during active trading.
if (Test-Path -LiteralPath (Join-Path $Home_ ".env")) {
    try {
        $services = & docker.exe compose -f (Join-Path $Home_ "compose.app.yml") ps --status running --services 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect running Docker services." }
        if ($services) { throw "Stop the paper-trading Docker app before upgrading." }
    } catch { throw "Cannot safely upgrade an older installation: $_" }
}

# 3. Download (or update) Trading Lab, keeping private files
Step "Downloading Trading Lab into $Home_"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("tradinglab-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
$backup = Join-Path $tmp "backup"
New-Item -ItemType Directory -Path $backup | Out-Null
$replacements = New-Object System.Collections.ArrayList
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
        $old = Join-Path $backup $item.Name
        $present = Test-Path -LiteralPath $dest
        if ($present) { Move-Item -LiteralPath $dest -Destination $old }
        [void]$replacements.Add(@{ Dest = $dest; Old = $old; Present = $present })
        Move-Item -LiteralPath $item.FullName -Destination $dest
    }
    Set-Content -LiteralPath (Join-Path $Home_ ".tradinglab-version") -Encoding ASCII -NoNewline -Value $revision
} catch {
    $failure = $_
    $rollbackFailed = $false
    for ($i = $replacements.Count - 1; $i -ge 0; $i--) {
        $item = $replacements[$i]
        try {
            if (Test-Path -LiteralPath $item.Dest) {
                Remove-Item -LiteralPath $item.Dest -Recurse -Force
            }
            if ($item.Present) { Move-Item -LiteralPath $item.Old -Destination $item.Dest }
        } catch { $rollbackFailed = $true }
    }
    if ($rollbackFailed) { throw "Installer rollback failed: backup remains at $backup. Details: $failure" }
    throw "Installer failed; prior program files restored. Details: $failure"
} finally {
    if (-not $rollbackFailed) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

# 4. Start it: login questions on first run, then the app window and the icons
Step "Starting Trading Lab (the first start builds it: allow 5 to 15 minutes)"
Set-Location $Home_
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Home_ "tools\launch_windows.ps1") -SkipUpdate
if ($LASTEXITCODE -ne 0) { throw "Trading Lab did not start; the message above says why." }
Write-Host "`nDone. Next time, open Trading Lab from its desktop or Start menu icon." -ForegroundColor Green
