param(
  [string]$KnowledgeBase = "E:\knowledge-base\voicevault",
  [Parameter(Mandatory = $true)]
  [string]$EventPath,
  [string]$Roles = "all"
)

$ErrorActionPreference = "Stop"

$doctorOutput = voicevault doctor --kb $KnowledgeBase 2>&1
$doctorExitCode = $LASTEXITCODE
$doctorOutput | Write-Host
if ($doctorExitCode -notin @(0, 1)) {
  throw "voicevault doctor failed with exit code $doctorExitCode"
}

voicevault build --kb $KnowledgeBase | Write-Host
$analysisResult = voicevault analyze --kb $KnowledgeBase --event $EventPath --roles $Roles --json | ConvertFrom-Json

$analysisJson = $analysisResult.analysis_json

if (-not (Test-Path -LiteralPath $analysisJson)) {
  throw "analysis.json not found: $analysisJson"
}

Write-Host $analysisJson
