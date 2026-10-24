# Trading Lab Windows launcher. Check/download latest code before starting the paper app.
param([switch]$SkipUpdate, [switch]$UpdateOnly, [switch]$CheckOnly)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VersionFile = Join-Path $Root ".tradinglab-version"
$Repository = "gahitchi/trading"

function Find-WorkingPython {
    # A valid py.exe can still point at an interpreter deleted from another user.
    $candidates = New-Object System.Collections.ArrayList
    $bases = @($env:LOCALAPPDATA, $env:ProgramFiles, [Environment]::GetEnvironmentVariable("ProgramFiles(x86)"))
    foreach ($base in $bases) {
        if (-not $base) { continue }
        foreach ($folder in @("Programs\Python\Python314", "Programs\Python\Python313",
                              "Python314", "Python313", "Python312", "Python311")) {
            $p = Join-Path $base "$folder\python.exe"
            if (Test-Path -LiteralPath $p -PathType Leaf) {
                [void]$candidates.Add(@{ File = $p; Prefix = @() })
            }
        }
    }
    foreach ($name in @("python.exe", "python3.exe")) {
        foreach ($hit in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($hit -and $hit.Source) {
                [void]$candidates.Add(@{ File = $hit.Source; Prefix = @() })
            }
        }
    }
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        [void]$candidates.Add(@{ File = "py.exe"; Prefix = @("-3.13") })
    }
    foreach ($candidate in $candidates) {
        try {
            $prefix = $candidate.Prefix
            $output = & $candidate.File @prefix -c "import sys; print(sys.executable); raise SystemExit(sys.version_info < (3, 10))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $output) { return $candidate }
        } catch { }
    }
    return $null
}

function Ensure-Python {
    $python = Find-WorkingPython
    if ($python) { return $python }
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw "No working Python found. Install Python 3.13 from python.org, or install Windows App Installer (winget)."
    }
    Write-Host "Installing Python 3.13: the registered Python launcher is unusable." -ForegroundColor Yellow
    & winget.exe install --exact --id Python.Python.3.13 --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed. Please install Python 3.13 and reopen Trading Lab." }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                 [Environment]::GetEnvironmentVariable("Path", "User")
    $python = Find-WorkingPython
    if (-not $python) { throw "Python installed but not discoverable. Sign out and back in, then reopen Trading Lab." }
    return $python
}

function App-IsRunning {
    # Unknown Docker state is not permission to replace executable code.
    if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) { return $true }
    try {
        $services = & docker.exe compose -f (Join-Path $Root "compose.app.yml") ps --status running --services 2>$null
        if ($LASTEXITCODE -ne 0) { return $true }
        return [bool](@($services | Where-Object { $_ -and $_.Trim() }).Count)
    } catch { return $true }
}

function Install-Version([string]$sha) {
    # Stage and back up on the same volume; never write to private data.
    $parent = Split-Path -Parent $Root
    $stage = Join-Path $parent ("TradingLab-stage-" + [guid]::NewGuid().ToString("N"))
    $backup = Join-Path $parent ("TradingLab-backup-" + [guid]::NewGuid().ToString("N"))
    $private = @(".env", "secrets", "results", "data", "research", "lab.toml", ".tradinglab-version")
    $changes = New-Object System.Collections.ArrayList
    $rollbackFailed = $false
    New-Item -ItemType Directory -Path $stage | Out-Null
    New-Item -ItemType Directory -Path $backup | Out-Null
    try {
        $zip = Join-Path $stage "source.zip"
        Invoke-WebRequest -Uri "https://github.com/$Repository/archive/$sha.zip" -OutFile $zip -UseBasicParsing
        $extract = Join-Path $stage "unpacked"
        Expand-Archive -LiteralPath $zip -DestinationPath $extract
        $source = Get-ChildItem -LiteralPath $extract -Directory | Select-Object -First 1
        if (-not $source -or -not (Test-Path (Join-Path $source.FullName "tools\start_app.py")) -or
            -not (Test-Path (Join-Path $source.FullName "tools\launch_windows.ps1")) -or
            -not (Test-Path (Join-Path $source.FullName "compose.app.yml"))) {
            throw "Incomplete downloaded Trading Lab archive."
        }
        foreach ($item in Get-ChildItem -LiteralPath $source.FullName -Force) {
            if ($private -contains $item.Name) { continue }
            $dest = Join-Path $Root $item.Name
            $old = Join-Path $backup $item.Name
            $hadOld = Test-Path -LiteralPath $dest
            if ($hadOld) { Move-Item -LiteralPath $dest -Destination $old }
            [void]$changes.Add(@{ Dest = $dest; Old = $old; HadOld = $hadOld })
            Move-Item -LiteralPath $item.FullName -Destination $dest
        }
        Set-Content -LiteralPath $VersionFile -Encoding Ascii -NoNewline -Value $sha
        Write-Host "Trading Lab updated to $($sha.Substring(0, 12)). Settings and history were preserved." -ForegroundColor Green
    } catch {
        $failure = $_
        for ($i = $changes.Count - 1; $i -ge 0; $i--) {
            $item = $changes[$i]
            try {
                if (Test-Path -LiteralPath $item.Dest) {
                    Remove-Item -LiteralPath $item.Dest -Recurse -Force
                }
                if ($item.HadOld) { Move-Item -LiteralPath $item.Old -Destination $item.Dest }
            } catch { $rollbackFailed = $true }
        }
        if ($rollbackFailed) {
            throw "Update failed and rollback was incomplete. Backup: $backup. Do not start trading. Details: $failure"
        }
        throw "Update failed; previous code restored. Details: $failure"
    } finally {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        if (-not $rollbackFailed) {
            Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Check-ForUpdate {
    try {
        $revision = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/commits/main" -Headers @{ "User-Agent" = "TradingLab-Windows-Launcher" }
        $sha = [string]$revision.sha
        if ($sha -notmatch '^[0-9a-f]{40}$') { throw "Unrecognized GitHub revision." }
    } catch {
        Write-Warning "Version check unavailable; keeping installed program. $_"
        return
    }
    $installed = if (Test-Path -LiteralPath $VersionFile) {
        (Get-Content -LiteralPath $VersionFile -Raw).Trim()
    } else { "unknown" }
    Write-Host "Installed version: $installed"
    Write-Host "Available revision: $sha"
    if ($sha -eq $installed) { Write-Host "Already up to date."; return }
    if ($CheckOnly) { Write-Host "An update is available; open Trading Lab to download it."; return }
    if (App-IsRunning) {
        Write-Warning "Update postponed: Trading Lab's Docker services are running or cannot be inspected. Stop them before updating."
        return
    }
    if (-not $UpdateOnly) {
        $response = Read-Host "Download and install the new version? (Y/n)"
        if ($response -and $response -notin @("y", "Y", "yes", "YES")) {
            Write-Host "Update skipped."
            return
        }
    }
    Install-Version $sha
}

try {
    if (-not $SkipUpdate) { Check-ForUpdate }
    if ($CheckOnly -or $UpdateOnly) { exit 0 }
    $python = Ensure-Python
    $prefix = $python.Prefix
    & $python.File @prefix (Join-Path $Root "tools\start_app.py")
    exit $LASTEXITCODE
} catch {
    Write-Host "Trading Lab launcher failed: $_" -ForegroundColor Red
    exit 1
}
