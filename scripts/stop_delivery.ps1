param(
    [Parameter(Mandatory=$true)][string]$Receipt,
    [int]$TimeoutSeconds = 30
)
$ErrorActionPreference = 'Stop'
if ($TimeoutSeconds -lt 1) { throw 'TimeoutSeconds must be positive.' }
$deliveryReceiptPath = (Resolve-Path -LiteralPath $Receipt).Path
$deliveryReceipt = Get-Content -LiteralPath $deliveryReceiptPath -Raw | ConvertFrom-Json
$deliveryRunPath = [IO.Path]::GetFullPath($deliveryReceipt.run)
$deliveryStopPath = [IO.Path]::GetFullPath($deliveryReceipt.stop_file)
if ((Split-Path -Parent $deliveryStopPath) -ne $deliveryRunPath -or (Split-Path -Leaf $deliveryStopPath) -ne 'stop.request') {
    throw 'Stop marker must belong to the recorded server run.'
}
$deliveryProcess = Get-Process -Id $deliveryReceipt.pid -ErrorAction SilentlyContinue
if ($null -eq $deliveryProcess) { Write-Output 'Recorded host already exited.'; return }
if ([IO.Path]::GetFullPath($deliveryProcess.Path) -ne [IO.Path]::GetFullPath($deliveryReceipt.host_binary)) {
    throw 'Recorded PID belongs to a different executable.'
}
[IO.File]::WriteAllText($deliveryStopPath, 'stop')
if (-not $deliveryProcess.WaitForExit($TimeoutSeconds * 1000)) {
    throw 'Host has not exited. Inspect the run logs; no process was forcibly terminated.'
}
Write-Output "Rust host exited. Inspect $deliveryRunPath\stdout.log and stderr.log for cleanup status."
