param(
    [ValidateSet('deepseek', 'openai', 'replay')]
    [string]$Provider = 'deepseek',
    [string]$Model = '',
    [ValidateRange(1, 10)]
    [int]$Repeat = 3,
    [string]$PythonExecutable = '',
    [string]$Output = '',
    [switch]$Resume,
    [switch]$CheckOnly,
    [switch]$PromptApiKey
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonExecutable) {
    $localPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
    $PythonExecutable = if (Test-Path -LiteralPath $localPython) { $localPython } else { 'python' }
}
if (-not $Output) {
    $Output = Join-Path $projectRoot ('eval-results/bfcl-' + $Provider + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$previousPythonPath = $env:PYTHONPATH
$temporaryApiKeyName = $null
$previousApiKey = $null
$exitCode = 0
Push-Location $projectRoot
try {
    $env:PYTHONPATH = (@((Join-Path $projectRoot 'src'), $previousPythonPath) | Where-Object { $_ }) -join [IO.Path]::PathSeparator
    # Check the interpreter and installed dependencies before starting the suite.
    # PYTHONPATH cannot repair a venv whose base interpreter was moved.
    $probe = 'import sys; import myagent.evaluation.bfcl; import openai; print(sys.executable)'
    try {
        $pythonPath = & $PythonExecutable -c $probe
        if ($LASTEXITCODE -ne 0) { throw "Python preflight exited with code $LASTEXITCODE" }
    }
    catch {
        throw "Cannot start the evaluation with '$PythonExecutable'. Repair .venv/pyvenv.cfg or pass -PythonExecutable with a working Python that has project dependencies. $($_.Exception.Message)"
    }
    Write-Host "Evaluation Python: $pythonPath"
    if ($CheckOnly) {
        & $PythonExecutable -m myagent.evaluation.bfcl validate
        exit $LASTEXITCODE
    }
    if ($Provider -ne 'replay') {
        $apiKeyName = if ($Provider -eq 'deepseek') { 'DEEPSEEK_API_KEY' } else { 'OPENAI_API_KEY' }
        $existingApiKey = [Environment]::GetEnvironmentVariable($apiKeyName, 'Process')
        if ($PromptApiKey -or [string]::IsNullOrWhiteSpace($existingApiKey)) {
            $temporaryApiKeyName = $apiKeyName
            $previousApiKey = $existingApiKey
            while ($true) {
                $secureApiKey = Read-Host "Enter $Provider API key (hidden input)" -AsSecureString
                $keyPointer = [IntPtr]::Zero
                $enteredApiKey = $null
                try {
                    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureApiKey)
                    $enteredApiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer).Trim()
                    if ([string]::IsNullOrWhiteSpace($enteredApiKey)) {
                        Write-Host 'API key cannot be empty. Please enter it again.'
                        continue
                    }
                    [Environment]::SetEnvironmentVariable($apiKeyName, $enteredApiKey, 'Process')
                    break
                }
                finally {
                    $enteredApiKey = $null
                    if ($keyPointer -ne [IntPtr]::Zero) {
                        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
                    }
                    $secureApiKey.Dispose()
                }
            }
        }
        $existingApiKey = $null
    }
    $common = @('--provider', $Provider, '--repeat', $Repeat)
    if ($Model) { $common += @('--model', $Model) }
    if ($Resume) { $common += '--resume' }

    # All 330 cases, each dimension counted independently.
    & $PythonExecutable -m myagent.evaluation.bfcl run @common --output (Join-Path $Output 'single')
    $singleExit = $LASTEXITCODE
    if ($singleExit -eq 2) { exit 2 }
    if ($singleExit -ne 0) { $exitCode = $singleExit }


}
finally {
    if ($temporaryApiKeyName) {
        [Environment]::SetEnvironmentVariable($temporaryApiKeyName, $previousApiKey, 'Process')
        $previousApiKey = $null
    }
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}
exit $exitCode
