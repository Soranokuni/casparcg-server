# Measures the idle CPU of the server with stdin at end-of-file (the way PlayOut starts it, without a
# console) and with a console stdin. On Windows the console thread retries getline() without pause
# when stdin is at end-of-file, so the first case uses about one CPU core.
param(
    [string]$ExeDir = (Join-Path $PSScriptRoot '..\..\build\shell\Release')
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$ExeDir = (Resolve-Path $ExeDir).Path

$template = Get-Content (Join-Path $here 'casparcg.config.template') -Raw
$cfg = Join-Path $here 'casparcg.config'
Set-Content -Path $cfg -Value ($template -replace '@TESTDIR@', ($here -replace '\\', '/')) -Encoding utf8
$empty = Join-Path $here 'empty.txt'
Set-Content -Path $empty -Value $null -NoNewline

function Measure-Idle([System.Diagnostics.Process]$p, [string]$label) {
    Start-Sleep -Seconds 6
    $p.Refresh()
    $t0 = $p.TotalProcessorTime.TotalSeconds
    Start-Sleep -Seconds 5
    $p.Refresh()
    $t1 = $p.TotalProcessorTime.TotalSeconds
    "{0}: {1:N2} CPU-seconds per wall second" -f $label, (($t1 - $t0) / 5)
}

$a = Start-Process -FilePath "$ExeDir\casparcg.exe" -ArgumentList "`"$cfg`"" -WorkingDirectory $ExeDir `
    -RedirectStandardInput $empty -RedirectStandardOutput (Join-Path $here 'out_eof.txt') `
    -WindowStyle Hidden -PassThru
try { Measure-Idle $a 'stdin at end-of-file' } finally { Stop-Process -Id $a.Id -Force }
Start-Sleep -Seconds 2

$b = Start-Process -FilePath "$ExeDir\casparcg.exe" -ArgumentList "`"$cfg`"" -WorkingDirectory $ExeDir `
    -WindowStyle Hidden -PassThru
try { Measure-Idle $b 'console stdin       ' } finally { Stop-Process -Id $b.Id -Force }
