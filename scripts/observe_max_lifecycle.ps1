# Logs Max process identity, native exit code and PID disappearance; never kills Max.
param([string]$OutputDirectory = "$env:LOCALAPPDATA\3dsmax-mcp\diagnostics\lifecycle")
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$ErrorActionPreference='Continue'
$log=Join-Path $OutputDirectory 'process-lifecycle.jsonl'
$tracked=@{}
while (-not (Test-Path -LiteralPath (Join-Path $OutputDirectory 'stop-observer'))) {
  $current=@(Get-Process 3dsmax -ErrorAction SilentlyContinue)
  foreach ($p in $current) {
    if (-not $tracked.ContainsKey($p.Id)) {
      try { $null=$p.Handle; $tracked[$p.Id]=$p; @{event='observed';pid=$p.Id;start=$p.StartTime.ToString('o');time=(Get-Date).ToString('o')} | ConvertTo-Json -Compress | Add-Content -LiteralPath $log } catch {}
    }
  }
  foreach ($processId in @($tracked.Keys)) {
    $p=$tracked[$processId]
    try {
      if ($p.HasExited) {
        if (@(Get-Process -Id $processId -ErrorAction SilentlyContinue).Count -gt 0) { continue }
        @{event='exit';pid=$processId;exit_code=$p.ExitCode;time=(Get-Date).ToString('o');still_listed=@(Get-Process -Id $processId -ErrorAction SilentlyContinue).Count} | ConvertTo-Json -Compress | Add-Content -LiteralPath $log
        $tracked.Remove($processId); $p.Dispose()
      }
    } catch { @{event='query_error';pid=$processId;message=$_.Exception.Message;time=(Get-Date).ToString('o')} | ConvertTo-Json -Compress | Add-Content -LiteralPath $log }
  }
  Start-Sleep -Milliseconds 500
}
foreach ($processId in @($tracked.Keys)) {
  $p=$tracked[$processId]
  try {
    if ($p.HasExited -and @(Get-Process -Id $processId -ErrorAction SilentlyContinue).Count -eq 0) {
      @{event='exit';pid=$processId;exit_code=$p.ExitCode;time=(Get-Date).ToString('o');still_listed=0} | ConvertTo-Json -Compress | Add-Content -LiteralPath $log
    }
  } finally { $p.Dispose() }
}
