<#
.SYNOPSIS
    Edit /root/setup-sniper/.env on the VPS without syncing secrets through git.

.DESCRIPTION
    Default: prompt for Alpaca news keys, upsert them on the server, recreate
    the container so cron inherits the new env, then print whether Alpaca
    news is configured (never prints key values).

    Secrets stay on the server. Deploy rsync excludes .env.

.PARAMETER RemoteHost
    Target host/IP. Falls back to $env:SNIPER_HOST, then 167.88.43.61.

.PARAMETER RemoteUser
    SSH user. Default "root".

.PARAMETER RemotePath
    App directory on the server. Default "/root/setup-sniper".

.PARAMETER SshKey
    Path to private key. Falls back to $env:SNIPER_SSH_KEY.

.PARAMETER KeyId
    Alpaca Key ID. Falls back to $env:APCA_API_KEY_ID, then a prompt.

.PARAMETER SecretKey
    Alpaca secret. Falls back to $env:APCA_API_SECRET_KEY, then a secure prompt.

.PARAMETER Editor
    Open nano on the remote .env instead of upserting keys.

.PARAMETER Status
    Report whether Alpaca keys are present on the server. Does not print values.

.PARAMETER DryRun
    Print remote commands without running them (never prints secrets).

.EXAMPLE
    .\edit-remote-env.ps1

.EXAMPLE
    .\edit-remote-env.ps1 -Editor

.EXAMPLE
    .\edit-remote-env.ps1 -Status
#>
[CmdletBinding()]
param(
    [string] $RemoteHost = $(if ($env:SNIPER_HOST) { $env:SNIPER_HOST } else { "167.88.43.61" }),
    [string] $RemoteUser = "root",
    [string] $RemotePath = "/root/setup-sniper",
    [string] $SshKey     = $env:SNIPER_SSH_KEY,
    [string] $KeyId      = $env:APCA_API_KEY_ID,
    [string] $SecretKey  = $env:APCA_API_SECRET_KEY,
    [switch] $Editor,
    [switch] $Status,
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

if (-not $RemoteHost) {
    throw "RemoteHost is required. Pass -RemoteHost or set `$env:SNIPER_HOST."
}

$Remote = "${RemoteUser}@${RemoteHost}"
$EnvFile = "$RemotePath/.env"

function Get-SshArgs {
    $a = @()
    if ($SshKey) { $a += @("-i", $SshKey) }
    $a += @("-o", "StrictHostKeyChecking=accept-new")
    return $a
}

function Invoke-Ssh {
    param(
        [Parameter(Mandatory)][string] $RemoteCmd,
        [switch] $Tty,
        [string] $Stdin
    )
    $sshArgs = @(Get-SshArgs)
    if ($Tty) { $sshArgs += "-t" }
    $sshArgs += @($Remote, $RemoteCmd)
    Write-Host "ssh $Remote <remote command>" -ForegroundColor DarkGray
    if ($DryRun) { return }
    if ($null -ne $Stdin -and $Stdin -ne "") {
        $Stdin | & ssh @sshArgs
    } else {
        & ssh @sshArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "ssh failed (exit $LASTEXITCODE)" }
}

function ConvertFrom-SecureToPlain([Security.SecureString] $Secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Invoke-RemotePythonStdin([string] $PythonSource, [string] $Stdin) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PythonSource))
    $cmd = "python3 -c `"import base64; exec(base64.b64decode('$b64').decode())`""
    Invoke-Ssh -RemoteCmd $cmd -Stdin $Stdin
}

function Invoke-RemotePython([string] $PythonSource) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PythonSource))
    Invoke-Ssh -RemoteCmd "python3 -c `"import base64; exec(base64.b64decode('$b64').decode())`""
}

$UpsertPy = @'
import pathlib, sys
path = pathlib.Path("__ENV_FILE__")
updates = {}
for raw in sys.stdin:
    line = raw.rstrip("\n")
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    k = k.strip()
    if k:
        updates[k] = v
if not updates:
    raise SystemExit("no KEY=VAL lines on stdin")
text = path.read_text(encoding="utf-8") if path.exists() else ""
lines = text.splitlines()
seen = set()
out = []
for line in lines:
    stripped = line.lstrip()
    if stripped and not stripped.startswith("#") and "=" in line:
        k = line.partition("=")[0].strip()
        if k in updates:
            out.append("%s=%s" % (k, updates[k]))
            seen.add(k)
            continue
    out.append(line)
for k, v in updates.items():
    if k not in seen:
        if out and out[-1] != "":
            out.append("")
        out.append("%s=%s" % (k, v))
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
print("updated", path, "keys:", ",".join(sorted(updates)))
'@
$UpsertPy = $UpsertPy.Replace("__ENV_FILE__", $EnvFile)

$StatusPy = @'
from pathlib import Path
p = Path("__ENV_FILE__")
vals = {}
if p.exists():
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        vals[k.strip()] = v
for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    v = vals.get(k, "")
    print("%s: %s" % (k, "set" if v.strip() else "missing"))
'@
$StatusPy = $StatusPy.Replace("__ENV_FILE__", $EnvFile)

Write-Host "Remote env: ${Remote}:${EnvFile}" -ForegroundColor Cyan

if ($Editor) {
    Write-Host "Opening nano on the server. Add APCA_API_KEY_ID and APCA_API_SECRET_KEY, save, exit." -ForegroundColor Yellow
    Invoke-Ssh -RemoteCmd "nano $EnvFile" -Tty
    Write-Host "Recreating container so cron picks up .env..." -ForegroundColor Yellow
    Invoke-Ssh -RemoteCmd "cd $RemotePath && docker compose up -d --force-recreate setup-sniper"
    Invoke-Ssh -RemoteCmd "docker logs setup-sniper 2>&1 | grep -i alpaca || true"
    return
}

if ($Status) {
    Invoke-RemotePython -PythonSource $StatusPy
    return
}

if (-not $KeyId) {
    $KeyId = Read-Host "APCA_API_KEY_ID"
}
if (-not $SecretKey) {
    $secure = Read-Host "APCA_API_SECRET_KEY" -AsSecureString
    $SecretKey = ConvertFrom-SecureToPlain $secure
}
if (-not $KeyId -or -not $SecretKey) {
    throw "Both APCA_API_KEY_ID and APCA_API_SECRET_KEY are required."
}

Write-Host "Upserting Alpaca keys on the server (values not logged)..." -ForegroundColor Yellow
$payload = "APCA_API_KEY_ID=$KeyId`nAPCA_API_SECRET_KEY=$SecretKey`n"
Invoke-RemotePythonStdin -PythonSource $UpsertPy -Stdin $payload

Write-Host "Recreating container so cron inherits the new env..." -ForegroundColor Yellow
Invoke-Ssh -RemoteCmd "cd $RemotePath && docker compose up -d --force-recreate setup-sniper"

Write-Host "Checking Alpaca status..." -ForegroundColor Yellow
Invoke-Ssh -RemoteCmd "docker logs setup-sniper 2>&1 | grep -i alpaca || true"
Invoke-RemotePython -PythonSource $StatusPy

Write-Host ""
Write-Host "Done. Next (optional):" -ForegroundColor Green
Write-Host "  ssh $Remote `"docker exec setup-sniper python main.py test`""
Write-Host "  ssh $Remote `"docker exec setup-sniper python main.py catalyst-backfill`""
