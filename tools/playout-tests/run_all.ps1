# Runs the PlayOut fork live tests: starts the built server with a test config, runs the three
# check scripts, stops the server. Prints the summary line of each script and every failed check.
#
#   powershell -File tools\playout-tests\run_all.ps1 [-ExeDir build\shell\Release] [-PlayOutDir D:\PlayOut]
param(
    [string]$ExeDir = (Join-Path $PSScriptRoot '..\..\build\shell\Release'),
    [string]$PlayOutDir = 'D:\PlayOut'
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$ExeDir = (Resolve-Path $ExeDir).Path

# The config needs absolute paths: write it from the template next to this script.
$template = Get-Content (Join-Path $here 'casparcg.config.template') -Raw
Set-Content -Path (Join-Path $here 'casparcg.config') -Value ($template -replace '@TESTDIR@', ($here -replace '\\', '/')) -Encoding utf8

if (-not (Test-Path (Join-Path $here 'media\clipa.mov'))) {
    python -I (Join-Path $here 'make_media.py') | Out-Null
}

# regress.py loads PlayOut's advisory template as a real CG ADD.
$advisory = Join-Path $PlayOutDir 'public\templates\playout\advisory.html'
if (Test-Path $advisory) {
    New-Item -ItemType Directory -Force (Join-Path $here 'template\playout') | Out-Null
    Copy-Item $advisory (Join-Path $here 'template\playout\advisory.html') -Force
}

Remove-Item (Join-Path $here 'rec*.mkv') -ErrorAction SilentlyContinue

$server = Start-Process -FilePath (Join-Path $ExeDir 'casparcg.exe') -ArgumentList "`"$here\casparcg.config`"" `
    -WorkingDirectory $ExeDir -WindowStyle Hidden -PassThru
try {
    Start-Sleep -Seconds 7
    foreach ($script in 'harness.py', 'extra.py', 'regress.py') {
        $log = Join-Path $here ($script -replace '\.py$', '.log')
        python -I (Join-Path $here $script) *> $log
        $summary = Select-String -Path $log -Pattern 'checks passed' | Select-Object -Last 1
        "{0,-12} {1}" -f $script, $summary.Line
        Select-String -Path $log -Pattern '^FAIL' | ForEach-Object { '    ' + $_.Line }
    }
} finally {
    Stop-Process -Id $server.Id -Force
}
