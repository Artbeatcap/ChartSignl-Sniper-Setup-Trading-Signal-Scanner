<#
.SYNOPSIS
    Push Setup Sniper to the production Linux server and rebuild the container.

.DESCRIPTION
    Windows-friendly equivalent of the manual flow in deploy/README.md:
        rsync repo -> /root/setup-sniper
        docker compose down && docker compose up -d --build
        optionally tail logs

    Transport order (first one that works):
        1. rsync inside WSL (preferred - matches README, supports --delete)
        2. native Windows rsync (Git-for-Windows / scoop)
        3. scp -r fallback (no --delete; slower on re-syncs)

    Secrets/runtime state are never synced. Configure .env on the server.

.PARAMETER RemoteHost
    Target host/IP. Falls back to $env:SNIPER_HOST.

.PARAMETER RemoteUser
    SSH user. Default "root".

.PARAMETER RemotePath
    Target directory on the server. Default "/root/setup-sniper".

.PARAMETER SshKey
    Path to private key. Falls back to $env:SNIPER_SSH_KEY. Optional (uses ssh-agent otherwise).

.PARAMETER SkipBuild
    Run `docker compose up -d` without `--build`.

.PARAMETER Logs
    Tail container logs after deploy.

.PARAMETER DryRun
    Run rsync with --dry-run; print remote commands without running them.

.EXAMPLE
    $env:SNIPER_HOST = "1.2.3.4"
    .\deploy\deploy.ps1

.EXAMPLE
    .\deploy\deploy.ps1 -SkipBuild -Logs

.EXAMPLE
    .\deploy\deploy.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string] $RemoteHost = $env:SNIPER_HOST,
    [string] $RemoteUser = "root",
    [string] $RemotePath = "/root/setup-sniper",
    [string] $SshKey     = $env:SNIPER_SSH_KEY,
    [switch] $SkipBuild,
    [switch] $Logs,
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

# PowerShell 7.4+ throws on native-command stderr when ErrorActionPreference=Stop;
# we want to inspect $LASTEXITCODE ourselves instead of turning wsl.conf warnings
# into fatal exceptions.
$PSNativeCommandUseErrorActionPreference = $false

# wsl.exe defaults to UTF-16 on some hosts, which mangles output when PowerShell
# reads it as UTF-8. Asking WSL to emit UTF-8 keeps wslpath/rsync output clean.
$env:WSL_UTF8 = "1"

# ── Paths ──────────────────────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$Compose   = Join-Path $RepoRoot "docker-compose.yml"
$Dockerfile = Join-Path $RepoRoot "Dockerfile"
$CrontabFile = Join-Path $ScriptDir "crontab"

if (-not $RemoteHost) {
    throw "RemoteHost is required. Pass -RemoteHost or set `$env:SNIPER_HOST."
}
foreach ($p in @($Compose, $Dockerfile)) {
    if (-not (Test-Path $p)) { throw "Missing required file: $p" }
}

$Remote = "${RemoteUser}@${RemoteHost}"
Write-Host "Deploying $RepoRoot -> ${Remote}:${RemotePath}" -ForegroundColor Cyan

# ── Normalize deploy/crontab line endings locally ──────────────────────
if (Test-Path $CrontabFile) {
    $raw = [System.IO.File]::ReadAllText($CrontabFile)
    if ($raw -match "`r`n") {
        Write-Host "Normalizing CRLF -> LF in deploy/crontab" -ForegroundColor DarkGray
        $lf = $raw -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText($CrontabFile, $lf)
    }
}

# ── Rsync exclude list (secrets, venvs, runtime state, IDE noise) ──────
$Excludes = @(
    ".env", ".env.*",
    "data/", "logs/", "*.log",
    "__pycache__/", "*.py[cod]",
    ".venv/", "venv/",
    ".git/", ".cursor/",
    ".pytest_cache/",
    "node_modules/",
    ".idea/", ".vscode/"
)

# ── Helpers ────────────────────────────────────────────────────────────
function Test-Cmd([string] $Name) {
    try { $null = Get-Command $Name -ErrorAction Stop; return $true } catch { return $false }
}

# PowerShell 5.1 converts native stderr into terminating errors when
# ErrorActionPreference=Stop, even with `*>$null`. Use this wrapper to
# silently probe for a native command exit code without tripping that.
function Invoke-NativeQuiet {
    param([Parameter(Mandatory)][scriptblock] $Script)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Script *> $null 2>&1 | Out-Null
    } finally {
        $ErrorActionPreference = $prev
    }
    return $LASTEXITCODE
}

function Get-SshArgs {
    $a = @()
    if ($SshKey) { $a += @("-i", $SshKey) }
    $a += @("-o", "StrictHostKeyChecking=accept-new")
    return $a
}

function ConvertTo-WslPath([string] $WinPath) {
    # Map "C:\Users\..." to "/mnt/c/Users/..." directly. Avoids the argument
    # escaping / wsl.conf warnings that `wsl wslpath` is prone to on some hosts.
    $full = (Resolve-Path -LiteralPath $WinPath).Path
    if ($full -match '^([A-Za-z]):\\?(.*)$') {
        $drive = $Matches[1].ToLower()
        $rest  = $Matches[2] -replace '\\', '/'
        return "/mnt/$drive/$rest"
    }
    throw "Cannot convert to WSL path: $full"
}

function Invoke-Ssh([string[]] $RemoteCmd) {
    $sshArgs = @(Get-SshArgs) + @($Remote) + $RemoteCmd
    Write-Host "ssh $($sshArgs -join ' ')" -ForegroundColor DarkGray
    if ($DryRun) { return }
    & ssh @sshArgs
    if ($LASTEXITCODE -ne 0) { throw "ssh failed (exit $LASTEXITCODE)" }
}

# ── Transport: rsync (WSL) > rsync (native) > scp ──────────────────────
function Invoke-RsyncWsl {
    Write-Host "Transport: rsync via WSL" -ForegroundColor Green
    $srcWsl = ConvertTo-WslPath $RepoRoot
    if (-not $srcWsl.EndsWith("/")) { $srcWsl += "/" }

    $sshCmd = "ssh -o StrictHostKeyChecking=accept-new"
    if ($SshKey) {
        $keyWsl = ConvertTo-WslPath $SshKey
        $sshCmd = "ssh -i `"$keyWsl`" -o StrictHostKeyChecking=accept-new"
    }

    $rsyncArgs = @("rsync", "-av", "--delete", "-e", $sshCmd)
    if ($DryRun) { $rsyncArgs += "--dry-run" }
    foreach ($e in $Excludes) { $rsyncArgs += @("--exclude", $e) }
    $rsyncArgs += @($srcWsl, "${Remote}:${RemotePath}/")

    Write-Host "wsl $($rsyncArgs -join ' ')" -ForegroundColor DarkGray
    & wsl @rsyncArgs
    if ($LASTEXITCODE -ne 0) { throw "rsync (wsl) failed (exit $LASTEXITCODE)" }
}

function Invoke-RsyncNative {
    Write-Host "Transport: native rsync" -ForegroundColor Green
    $src = $RepoRoot
    if (-not $src.EndsWith("\")) { $src += "\" }

    $sshCmd = "ssh -o StrictHostKeyChecking=accept-new"
    if ($SshKey) { $sshCmd = "ssh -i `"$SshKey`" -o StrictHostKeyChecking=accept-new" }

    $rsyncArgs = @("-av", "--delete", "-e", $sshCmd)
    if ($DryRun) { $rsyncArgs += "--dry-run" }
    foreach ($e in $Excludes) { $rsyncArgs += @("--exclude", $e) }
    $rsyncArgs += @($src, "${Remote}:${RemotePath}/")

    Write-Host "rsync $($rsyncArgs -join ' ')" -ForegroundColor DarkGray
    & rsync @rsyncArgs
    if ($LASTEXITCODE -ne 0) { throw "rsync failed (exit $LASTEXITCODE)" }
}

function Test-Excluded([string] $Name) {
    foreach ($p in $Excludes) {
        $pat = $p.TrimEnd('/')
        if ($Name -like $pat) { return $true }
    }
    return $false
}

function Invoke-Scp {
    Write-Host "Transport: scp (fallback; no --delete semantics)" -ForegroundColor Yellow
    Invoke-Ssh @("mkdir", "-p", $RemotePath)

    $items = Get-ChildItem -LiteralPath $RepoRoot -Force |
        Where-Object { -not (Test-Excluded $_.Name) }

    $scpBase = @()
    if ($SshKey) { $scpBase += @("-i", $SshKey) }
    $scpBase += @("-o", "StrictHostKeyChecking=accept-new", "-r")

    foreach ($i in $items) {
        $srcArg = $i.FullName
        $dstArg = "${Remote}:${RemotePath}/"
        Write-Host "scp $($scpBase -join ' ') `"$srcArg`" `"$dstArg`"" -ForegroundColor DarkGray
        if ($DryRun) { continue }
        & scp @scpBase $srcArg $dstArg
        if ($LASTEXITCODE -ne 0) { throw "scp failed for $srcArg (exit $LASTEXITCODE)" }
    }
}

function Test-Wsl {
    if (-not (Test-Cmd "wsl")) { return $false }
    if ((Invoke-NativeQuiet { wsl --status }) -ne 0) { return $false }
    return ((Invoke-NativeQuiet { wsl rsync --version }) -eq 0)
}

# ── Main ───────────────────────────────────────────────────────────────
try {
    if (Test-Wsl) {
        Invoke-RsyncWsl
    } elseif (Test-Cmd "rsync") {
        Invoke-RsyncNative
    } elseif (Test-Cmd "scp") {
        Invoke-Scp
    } else {
        throw "No transport available. Install WSL (with rsync), rsync, or OpenSSH scp."
    }

    $buildFlag = if ($SkipBuild) { "" } else { " --build" }
    $remoteCmd = "cd $RemotePath && docker compose down && docker compose up -d$buildFlag && docker compose ps"
    Invoke-Ssh @($remoteCmd)

    if ($Logs) {
        if ($DryRun) {
            Write-Host "ssh $Remote 'cd $RemotePath && docker compose logs -f --tail=100 setup-sniper'" -ForegroundColor DarkGray
        } else {
            $sshArgs = @(Get-SshArgs) + @($Remote, "cd $RemotePath && docker compose logs -f --tail=100 setup-sniper")
            & ssh @sshArgs
        }
    }

    Write-Host "Deploy complete." -ForegroundColor Green
}
catch {
    Write-Host "Deploy failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
