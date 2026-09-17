<#
.SYNOPSIS
    Bring the TradeForge stack up or down together with the MetaTrader collector agent.

.DESCRIPTION
    The containers (Postgres, Redis, API, worker, web) run in Docker. The collector agent cannot:
    the MetaTrader5 library is Windows-only (ADR-0021), so it runs on this machine beside the
    terminal. This script ties the two lifetimes together.

      up      Start Docker Desktop if needed, `docker compose up -d`, wait for Redis, then start
              the agent in the background (no window) with its output in data\logs.
      down    `docker compose down`, then wait for the agent to stop by itself: it exits once the
              stack's Redis has been silent for its grace period (PR-259). Only an agent that
              has not stopped by then is ended by force, and the script says so.
      status  The containers, whether the agent is alive, the collection queue and the agent's
              last log lines.

    The agent watches the stack's Redis, not the stack: it also stops by itself when Redis goes
    away some other way (`docker compose down` typed by hand), once its grace period has passed.
    A collection still downloading holds it until that collection ends. `docker compose stop` of
    other services leaves it running.

.PARAMETER Build
    With `up`: rebuild the images before starting them.

.PARAMETER ServerOffset
    The broker's clock in hours ahead of UTC, handed to the agent as TRADEFORGE_SERVER_OFFSET.
    Needed to connect while the market is closed. Defaults to that environment variable, and
    to +3 -- the value every collector command in docs\aulas\RETOMAR.md uses -- when it is unset.

.EXAMPLE
    .\tradeforge.ps1 up
    .\tradeforge.ps1 up -Build
    .\tradeforge.ps1 status
    .\tradeforge.ps1 down

.NOTES
    Run `up` from a terminal. When another program captures this script's output through an OS
    pipe -- `powershell -File tradeforge.ps1 up | ...` from cmd or bash, a CI step, a scheduler --
    that program does not see the end of the output until the collector agent exits: the agent is
    started with inherited handles (needed to redirect its output to the log files) and so holds a
    copy of the pipe's write end. Measured: the reader waited the child's whole lifetime.
    A PowerShell pipeline inside one session (`.\tradeforge.ps1 up | Select-String ...`) is not an
    OS pipe and returns at once, and so does redirecting the output to a file.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('up', 'down', 'status')]
    [string]$Command,

    [switch]$Build,

    [string]$ServerOffset
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'data\logs'
$PidFile = Join-Path $Root 'data\collector-agent.pid'
$AgentLog = Join-Path $LogDir 'collector-agent.log'
$AgentErr = Join-Path $LogDir 'collector-agent.err.log'
$DockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'

# The agent's grace (lifetime.DEFAULT_GRACE, 15 s), its confirming ping and the time it gives its
# worker to close (supervisor CLOSE_TIMEOUT, 5 s): about 23 s at worst, and its clock already
# runs while `docker compose down` is still stopping the other containers.
$AgentStopWaitSeconds = 25
# Marks the agent's command line, so a PID reused by some other process is never mistaken for it.
$AgentMarker = 'tradeforge_collector.cli'

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Native([scriptblock]$Block) {
    # Windows PowerShell 5.1 turns a native command's stderr into an error record, and under
    # ErrorActionPreference Stop that ends the script -- even with `2> $null` (measured). Callers
    # here expect failures and read $LASTEXITCODE themselves, so run the command under Continue.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Block
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-Docker {
    # `docker info` fails when the daemon is down; only the exit code matters here.
    Invoke-Native { docker info *> $null }
    return $LASTEXITCODE -eq 0
}

function Start-DockerIfNeeded {
    if (Test-Docker) { return }
    if (-not (Test-Path $DockerDesktop)) {
        throw "Docker is not running and Docker Desktop was not found at $DockerDesktop."
    }
    Write-Step 'Starting Docker Desktop'
    Start-Process -FilePath $DockerDesktop | Out-Null
    $deadline = (Get-Date).AddMinutes(3)
    while (-not (Test-Docker)) {
        if ((Get-Date) -gt $deadline) { throw 'Docker did not come up within 3 minutes.' }
        Start-Sleep -Seconds 3
    }
}

function Wait-RedisHealthy {
    $deadline = (Get-Date).AddMinutes(2)
    while ($true) {
        $health = Invoke-Native { docker inspect --format '{{.State.Health.Status}}' tradeforge-redis 2> $null }
        if ($health -eq 'healthy') { return }
        if ((Get-Date) -gt $deadline) { throw "Redis is not healthy after 2 minutes (state: $health)." }
        Start-Sleep -Seconds 2
    }
}

function Get-Agent {
    # The running agent, or $null. Trusts the PID file only when that PID is still our command.
    # A file that does not hold a PID (emptied by an interrupted `up`, edited by hand) is stale too.
    if (-not (Test-Path $PidFile)) { return $null }
    $agentId = 0
    $parsed = [int]::TryParse([string](Get-Content $PidFile -Raw), [ref]$agentId)
    $process = $null
    if ($parsed -and $agentId -gt 0) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $agentId" -ErrorAction SilentlyContinue
    }
    if ($null -eq $process -or $process.CommandLine -notlike "*$AgentMarker*") {
        Remove-Item $PidFile -Force
        return $null
    }
    return Get-Process -Id $agentId -ErrorAction SilentlyContinue
}

function Start-Agent {
    $running = Get-Agent
    if ($null -ne $running) {
        Write-Step "Collector agent already running (PID $($running.Id))"
        return
    }
    if (-not (Test-Path $Python)) { throw "No virtual environment at $Python -- run 'uv sync' first." }

    $offset = $ServerOffset
    if (-not $offset) { $offset = $env:TRADEFORGE_SERVER_OFFSET }
    if (-not $offset) { $offset = '+3' }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    # The agent inherits this process's environment. Set the two variables only around the start
    # and put the old values back, so a later `up` in the same session does not reuse this offset.
    $previousOffset = $env:TRADEFORGE_SERVER_OFFSET
    $previousUtf8 = $env:PYTHONUTF8
    $env:TRADEFORGE_SERVER_OFFSET = $offset
    # The log is written in UTF-8 whatever the console's code page; `status` reads it the same way.
    $env:PYTHONUTF8 = '1'
    # Started with the venv's python, not through `uv run`: one launcher fewer between this
    # script and the agent. Even so the venv's python.exe is itself a launcher -- CPython's own
    # venv launcher, copied in by uv -- that runs the real interpreter as its child (measured: two
    # processes with this command line). The PID written below is the launcher's; it lives exactly
    # as long as the agent, so waiting on it is right, and Stop-Agent ends the whole tree when it
    # has to use force.
    $arguments = @('-c', "`"import sys; from $AgentMarker import main; sys.exit(main(['agent']))`"")
    try {
        $agent = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Root `
            -WindowStyle Hidden -RedirectStandardOutput $AgentLog -RedirectStandardError $AgentErr `
            -PassThru
    } finally {
        $env:TRADEFORGE_SERVER_OFFSET = $previousOffset
        $env:PYTHONUTF8 = $previousUtf8
    }
    # Holding the handle keeps ExitCode readable once the process is gone (a .NET caveat).
    $null = $agent.Handle
    Set-Content -Path $PidFile -Value $agent.Id -Encoding ascii

    # The agent refuses to start without Redis (status 2); give it a moment to say so.
    Start-Sleep -Seconds 3
    if ($agent.HasExited) {
        Remove-Item $PidFile -Force
        throw "The collector agent stopped at once (status $($agent.ExitCode)). See $AgentErr."
    }
    Write-Step "Collector agent running (PID $($agent.Id), server offset $offset). Log: $AgentErr"
}

function Stop-Agent {
    $agent = Get-Agent
    if ($null -eq $agent) {
        Write-Step 'Collector agent was not running'
        return
    }
    Write-Step "Waiting up to $AgentStopWaitSeconds s for the collector agent to stop by itself"
    if ($agent.WaitForExit($AgentStopWaitSeconds * 1000)) {
        Write-Step 'Collector agent stopped by itself'
    } else {
        # A collection that is still downloading holds the agent until it ends; force is the
        # only way to stop that now, and it is said out loud.
        # /T takes the launcher and the interpreter it started. Ending the launcher alone also ends
        # its child today (measured: uv's launcher ties the two together), but /T does not rely on
        # a detail of the launcher.
        Invoke-Native { taskkill /PID $agent.Id /T /F *> $null }
        if ($LASTEXITCODE -ne 0 -and -not $agent.HasExited) {
            throw "The collector agent did not stop within $AgentStopWaitSeconds s and taskkill could not end it (PID $($agent.Id))."
        }
        Write-Warning "The collector agent did not stop within $AgentStopWaitSeconds s and was ended by force."
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

function Show-Status {
    if (Test-Docker) {
        docker compose ps --format 'table {{.Name}}\t{{.Status}}'
        # Right after `down` the container is gone and this fails; the rest of the status still shows.
        $queued = Invoke-Native { docker exec tradeforge-redis redis-cli ZCARD collect 2> $null }
        if ($LASTEXITCODE -ne 0) { $queued = $null }
        if ($queued) { Write-Host "Collection jobs queued: $queued" }
    } else {
        Write-Host 'Docker is not running.'
    }

    $agent = Get-Agent
    if ($null -eq $agent) {
        Write-Host 'Collector agent: not running'
    } else {
        Write-Host "Collector agent: running (PID $($agent.Id), since $($agent.StartTime))"
    }
    if (Test-Path $AgentErr) {
        Write-Host "Last lines of ${AgentErr}:"
        Get-Content $AgentErr -Tail 5 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
    }
}

Push-Location $Root
try {
    switch ($Command) {
        'up' {
            Start-DockerIfNeeded
            Write-Step 'docker compose up -d'
            if ($Build) { docker compose up -d --build } else { docker compose up -d }
            if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed.' }
            # A recreated `api` comes back on a new IP, and the web container's nginx resolved the
            # old one when it started: the screens answer 502 with every container healthy.
            # Restarting web costs a second and makes that impossible.
            Write-Step 'docker compose restart web'
            docker compose restart web
            if ($LASTEXITCODE -ne 0) { throw 'docker compose restart web failed.' }
            Write-Step 'Waiting for Redis'
            Wait-RedisHealthy
            Start-Agent
        }
        'down' {
            if (Test-Docker) {
                Write-Step 'docker compose down'
                docker compose down
                if ($LASTEXITCODE -ne 0) { throw 'docker compose down failed.' }
            }
            Stop-Agent
        }
        'status' {
            Show-Status
        }
    }
} finally {
    Pop-Location
}
# Reaching here is success. Without this, a caller in the same session would read the exit code
# of the last native command run above -- `docker info` failing on purpose when Docker is down.
exit 0
