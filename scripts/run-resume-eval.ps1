[CmdletBinding()]
param(
    [string]$Python,
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"

function Resolve-PythonExecutable {
    param([string]$RequestedPython)

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($RequestedPython)) {
        $candidates += $RequestedPython
    }
    if (-not [string]::IsNullOrWhiteSpace($env:MYAGENT_PYTHON)) {
        $candidates += $env:MYAGENT_PYTHON
    }

    $candidates += (Join-Path $script:RepoRoot ".venv\Scripts\python.exe")

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand -and -not [string]::IsNullOrWhiteSpace($pythonCommand.Source)) {
        $candidates += $pythonCommand.Source
    }

    $candidates += "D:\soft\Python312\cpython-3.12-windows-x86_64-none\python.exe"

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try {
            & $candidate -c "import sys; print(sys.executable)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }

    throw "No usable Python interpreter was found. Pass one with -Python or set MYAGENT_PYTHON."
}

function Invoke-EvaluationCommand {
    param(
        [string]$Step,
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host "==> $Step" -ForegroundColor Cyan
    & $script:PythonExecutable -m myagent.evaluation @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

$script:RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $script:RepoRoot

$script:PythonExecutable = Resolve-PythonExecutable -RequestedPython $Python
$dataset = Join-Path $script:RepoRoot "evaluations\live\resume-cases.jsonl"

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputDirectory = Join-Path $script:RepoRoot "eval-results\deepseek-v4-flash-0731-$timestamp"
}
elseif (-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $script:RepoRoot $OutputDirectory
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$smokeDirectory = Join-Path $OutputDirectory "smoke"

if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    $secureKey = Read-Host "Enter DEEPSEEK_API_KEY (input is hidden)" -AsSecureString
    $env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
}
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    throw "DEEPSEEK_API_KEY cannot be empty."
}

$pythonPathEntries = @(
    (Join-Path $script:RepoRoot "src"),
    (Join-Path $script:RepoRoot ".venv\Lib\site-packages")
)
if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $pythonPathEntries += $env:PYTHONPATH
}
$env:PYTHONPATH = $pythonPathEntries -join [System.IO.Path]::PathSeparator

Write-Host "Repository: $script:RepoRoot"
Write-Host "Python:     $script:PythonExecutable"
Write-Host "Output:     $OutputDirectory"

Invoke-EvaluationCommand -Step "Validate resume-eval-v1 dataset" -Arguments @(
    "validate",
    "--dataset", $dataset
)

Invoke-EvaluationCommand -Step "Run one-case DeepSeek protocol smoke" -Arguments @(
    "run",
    "--provider", "deepseek",
    "--model", "deepseek-v4-flash",
    "--release-label", "DeepSeek-V4-Flash-0731",
    "--repeat", "1",
    "--thinking", "enabled",
    "--case", "component_no_tool_answer",
    "--dataset", $dataset,
    "--allow-code-execution",
    "--output", $smokeDirectory
)

Invoke-EvaluationCommand -Step "Run 16 cases x 3 attempts (48 live runs)" -Arguments @(
    "run",
    "--provider", "deepseek",
    "--model", "deepseek-v4-flash",
    "--release-label", "DeepSeek-V4-Flash-0731",
    "--repeat", "3",
    "--thinking", "enabled",
    "--dataset", $dataset,
    "--allow-code-execution",
    "--output", $OutputDirectory
)

$judgePackets = Join-Path $OutputDirectory "judge-packets.jsonl"
$summary = Join-Path $OutputDirectory "summary.json"

Write-Host ""
Write-Host "Evaluation runs completed. Judge has not been run." -ForegroundColor Green
Write-Host "Result directory: $OutputDirectory"
Write-Host "Judge packets:   $judgePackets"
Write-Host "Run summary:     $summary"
Write-Host ""
Write-Host "Keep this directory unchanged, then send the Result directory path to Codex for judging."
