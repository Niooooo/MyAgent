param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$gateExitCode = 0

Push-Location $projectRoot
try {
    $env:PYTHONPATH = Join-Path $projectRoot "src"
    $env:PYTHONDONTWRITEBYTECODE = "1"

    & $PythonExecutable -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        $gateExitCode = $LASTEXITCODE
    }

    if ($gateExitCode -eq 0) {
        & $PythonExecutable -m myagent.evaluation validate `
            --dataset evaluations/live/cases.jsonl
        if ($LASTEXITCODE -ne 0) {
            $gateExitCode = $LASTEXITCODE
        }
    }

    if ($gateExitCode -eq 0) {
        & $PythonExecutable -m myagent.evaluation run `
            --dataset evaluations/live/cases.jsonl `
            --provider replay `
            --allow-code-execution `
            --output eval-results/replay
        if ($LASTEXITCODE -ne 0) {
            $gateExitCode = $LASTEXITCODE
        }
    }
}
finally {
    Pop-Location
}

exit $gateExitCode
