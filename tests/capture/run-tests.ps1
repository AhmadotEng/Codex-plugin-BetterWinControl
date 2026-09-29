$ErrorActionPreference='Stop'
$captureResult=Join-Path $PSScriptRoot 'results.json'
$captureStart=[Diagnostics.ProcessStartInfo]::new((Get-Command dotnet).Source)
$captureStart.UseShellExecute=$false
$captureStart.CreateNoWindow=$true
$captureStart.RedirectStandardOutput=$true
$captureStart.RedirectStandardError=$true
$captureStart.ArgumentList.Add((Join-Path $PSScriptRoot 'bin/Release/net10.0-windows10.0.22000.0/CapturePreviewTests.dll'))
$captureStart.ArgumentList.Add($captureResult)
$captureProcess=[Diagnostics.Process]::Start($captureStart)
$captureOut=$captureProcess.StandardOutput.ReadToEndAsync()
$captureErr=$captureProcess.StandardError.ReadToEndAsync()
try {
    if(-not $captureProcess.WaitForExit(18000)) {
        $captureProcess.Kill($true)
        [void]$captureProcess.WaitForExit(1000)
        @{status='failed';failure='External18-second watchdog terminated own test process.'}|ConvertTo-Json|Set-Content -LiteralPath $captureResult
        throw 'Capture test timeout.'
    }
    $captureOut.GetAwaiter().GetResult()|Write-Output
    $captureErrorText=$captureErr.GetAwaiter().GetResult()
    if($captureErrorText){Write-Output $captureErrorText}
    if($captureProcess.ExitCode-ne 0){throw "Capture tests failed: $captureResult"}
}finally{if(-not $captureProcess.HasExited){$captureProcess.Kill($true)};$captureProcess.Dispose()}
