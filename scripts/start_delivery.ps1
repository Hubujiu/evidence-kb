param(
    [string]$Config = 'config/delivery-local-v1.json',
    [string]$Providers = '.local/delivery-20261026-01/providers.json',
    [string]$RunRoot = '.local/delivery-20261026-01',
    [string]$SemanticRuntime = '.local/delivery-20261026-01/postgres-semantic-runtime.json',
    [string]$BudgetBatch = '',
    [switch]$CheckOnly,
    [switch]$StartDependencies
)
$ErrorActionPreference = 'Stop'
$deliveryWorkspace = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $deliveryWorkspace
$deliveryPython = Join-Path $deliveryWorkspace '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $deliveryPython -PathType Leaf)) { throw 'Missing project .venv Python.' }
foreach ($deliveryFile in @($Config, $Providers, $SemanticRuntime)) {
    if (-not (Test-Path -LiteralPath $deliveryFile -PathType Leaf)) { throw "Missing configuration file: $deliveryFile" }
}
$deliveryConfig = (Resolve-Path -LiteralPath $Config).Path
$deliveryProviders = (Resolve-Path -LiteralPath $Providers).Path
$deliverySemanticRuntime = (Resolve-Path -LiteralPath $SemanticRuntime).Path
$deliveryRoot = if ([IO.Path]::IsPathRooted($RunRoot)) { [IO.Path]::GetFullPath($RunRoot) } else { [IO.Path]::GetFullPath((Join-Path $deliveryWorkspace $RunRoot)) }
$deliveryPort = 58089
$env:PYTHONIOENCODING = 'utf-8'
# Always select this delivery's explicit runtime, including in a fresh shell.
# The application factory owns provider/manifest validation; never inherit Jina.
$env:KB_SEMANTIC_PAGE_CONFIG = $deliverySemanticRuntime
if ($BudgetBatch) {
    if (-not (Test-Path -LiteralPath $BudgetBatch -PathType Leaf)) { throw 'Missing explicit runtime budget batch.' }
    $env:KB_DELIVERY_BUDGET_BATCH = (Resolve-Path -LiteralPath $BudgetBatch).Path
} else {
    Remove-Item Env:KB_DELIVERY_BUDGET_BATCH -ErrorAction SilentlyContinue
}
& $deliveryPython -B -m scripts.delivery_server_app --check --config $deliveryConfig --providers $deliveryProviders --run-root $deliveryRoot
if ($LASTEXITCODE -ne 0) { throw 'Delivery configuration validation failed; no service started.' }
if ($CheckOnly) { return }
$deliveryDocument = Get-Content -LiteralPath $deliveryConfig -Raw | ConvertFrom-Json
$deliveryListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $deliveryPort)
try { $deliveryListener.Start() }
catch { throw "Port $deliveryPort is occupied. No existing process was stopped." }
finally { $deliveryListener.Stop() }
$deliveryRun = Join-Path $deliveryRoot ('server-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $deliveryRun | Out-Null
if ($StartDependencies) {
    $deliveryDocker = Start-Process -FilePath 'docker.exe' -ArgumentList @('compose', 'up', '-d', '--wait', 'postgres') `
        -WorkingDirectory $deliveryWorkspace -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $deliveryRun 'dependencies.stdout.log') `
        -RedirectStandardError (Join-Path $deliveryRun 'dependencies.stderr.log')
    # Retain the process handle before exit so Windows PowerShell can read ExitCode.
    $deliveryDockerHandle = $deliveryDocker.Handle
    if (-not $deliveryDocker.WaitForExit(180000)) { throw "Dependency command still running; owned PID $($deliveryDocker.Id). Inspect its logs before retrying." }
    $deliveryDocker.WaitForExit()
    if ($deliveryDocker.ExitCode -ne 0) { throw "Dependency startup failed. Inspect $deliveryRun" }
}
$env:KB_DELIVERY_CONFIG = $deliveryConfig
$env:KB_DELIVERY_PROVIDERS = $deliveryProviders
$env:KB_DELIVERY_ROOT = $deliveryRoot
$env:PYTHONIOENCODING = 'utf-8'
$deliveryProcess = Start-Process -FilePath $deliveryPython `
    -ArgumentList @('-B', '-m', 'uvicorn', 'kb_platform.delivery:create_delivery_app', '--factory', '--host', '127.0.0.1', '--port', "$deliveryPort", '--workers', '1') `
    -WorkingDirectory $deliveryWorkspace -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $deliveryRun 'stdout.log') `
    -RedirectStandardError (Join-Path $deliveryRun 'stderr.log')
$deliveryReceipt = @{pid=$deliveryProcess.Id; port=$deliveryPort; run=$deliveryRun; profile=$deliveryDocument.id;
    config=$deliveryConfig; providers=$deliveryProviders; identities=(Join-Path $deliveryRoot 'identities.json');
    semantic_runtime=$deliverySemanticRuntime;
    semantic_runtime_sha256=(Get-FileHash -LiteralPath $deliverySemanticRuntime -Algorithm SHA256).Hash.ToLowerInvariant();
    budget_batch=$env:KB_DELIVERY_BUDGET_BATCH;
    started_at=(Get-Date).ToUniversalTime().ToString('o'); status='STARTING'; token_printed=$false}
$deliveryReceipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $deliveryRun 'process.json') -Encoding utf8
$deliveryReady = $false
for ($deliveryAttempt = 0; $deliveryAttempt -lt 20; $deliveryAttempt++) {
    $deliveryProcess.Refresh()
    if ($deliveryProcess.HasExited) { throw "Owned service exited. Inspect $deliveryRun\stderr.log" }
    try {
        $deliveryResponse = Invoke-WebRequest -Uri "http://127.0.0.1:$deliveryPort/health" -TimeoutSec 2 -UseBasicParsing
        if ($deliveryResponse.StatusCode -eq 200 -and $deliveryResponse.Headers['X-Delivery-Profile'] -eq $deliveryDocument.id) {
            $deliveryReady = $true
            break
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $deliveryReady) { throw "Owned service did not confirm readiness yet; PID $($deliveryProcess.Id), logs $deliveryRun. No process was stopped." }
$deliveryReceipt.status = 'RUNNING_LIVENESS_ONLY'
$deliveryReceipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $deliveryRun 'process.json') -Encoding utf8
Write-Output "Delivery process running: PID $($deliveryProcess.Id), http://127.0.0.1:$deliveryPort. Check authenticated dependency readiness in the conversation page. Private identity: $(Join-Path $deliveryRoot 'identities.json')."
