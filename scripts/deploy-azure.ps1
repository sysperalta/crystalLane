<#
.SYNOPSIS
  Deploys the adapter to Azure Container Apps as a single container (TMS simulator embedded).

.DESCRIPTION
  Safe to re-run. The first run creates:
    - resource group, user-assigned managed identity with AcrPull on the registry
    - Container Apps environment (consumption plan, no fixed cost) with its Log Analytics workspace
    - the container app: HTTPS ingress, /healthz probes, exactly 1 replica (call state is in memory)
  Later runs build and push a new image tag and roll a new revision.

  Secrets (API_KEY, TMS_TOKEN, DEMO_CONSOLE_KEY) are generated once into .env.azure next to the
  project root and are never printed. BASE_URL is written there after the deploy.

  The target subscription and container registry are read from .env.azure (AZURE_SUBSCRIPTION,
  AZURE_REGISTRY) so they never live in the repository; -Subscription and -Registry override them.

.EXAMPLE
  .\scripts\deploy-azure.ps1
.EXAMPLE
  .\scripts\deploy-azure.ps1 -Registry myregistry -ResourceGroup rg-other
#>
param(
  [string]$Subscription = "",  # default: AZURE_SUBSCRIPTION in .env.azure, else the current az account

  [string]$Location = "eastus",
  [string]$ResourceGroup = "rg-clf-adapter-demo",
  [string]$Environment = "cae-clf-adapter-demo",
  [string]$App = "ca-clf-adapter",
  [string]$Identity = "id-clf-adapter",
  [string]$Registry = "",      # default: AZURE_REGISTRY in .env.azure (an existing Azure Container Registry)
  [string]$Repository = "clf-adapter",
  [string]$DemoCarriers = "123456:+15551234821,654321:+15559876543",
  [string]$SecretsFile = (Join-Path $PSScriptRoot "..\.env.azure")
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Step([string]$Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }

# Native tools write progress to stderr; only their exit code decides failure.
function Invoke-Az {
  $ErrorActionPreference = "Continue"
  $output = & az @args --only-show-errors
  if ($LASTEXITCODE -ne 0) { throw "az $($args[0]) $($args[1]) failed (exit $LASTEXITCODE)" }
  return $output
}

function Invoke-Docker {
  $ErrorActionPreference = "Continue"
  & docker @args
  if ($LASTEXITCODE -ne 0) { throw "docker $($args[0]) failed (exit $LASTEXITCODE)" }
}

function New-Secret([int]$Bytes = 24) {
  $buffer = New-Object byte[] $Bytes
  $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
  $rng.GetBytes($buffer); $rng.Dispose()
  return ($buffer | ForEach-Object { $_.ToString("x2") }) -join ""
}

function Read-DotEnv([string]$Path) {
  $vars = @{}
  foreach ($line in Get-Content $Path) {
    if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') { $vars[$Matches[1]] = $Matches[2] }
  }
  return $vars
}

function Set-DotEnvValue([string]$Path, [string]$Name, [string]$Value) {
  $lines = @(Get-Content $Path | Where-Object { $_ -notmatch "^$Name=" }) + "$Name=$Value"
  [IO.File]::WriteAllText($Path, ($lines -join "`r`n") + "`r`n", (New-Object Text.UTF8Encoding $false))
}

# Deployment targets stay out of the repository: read them from .env.azure when not passed.
if (Test-Path $SecretsFile) {
  $targets = Read-DotEnv $SecretsFile
  if (-not $Subscription) { $Subscription = $targets.AZURE_SUBSCRIPTION }
  if (-not $Registry) { $Registry = $targets.AZURE_REGISTRY }
}
if (-not $Registry) { throw "No container registry: pass -Registry or set AZURE_REGISTRY in $SecretsFile" }

Step "Subscription"
if ($Subscription) { Invoke-Az account set --subscription $Subscription | Out-Null }
Write-Host ("   " + (Invoke-Az account show --query name -o tsv))

Step "Resource providers"
foreach ($ns in "Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.ManagedIdentity") {
  $state = Invoke-Az provider show -n $ns --query registrationState -o tsv
  if ($state -ne "Registered") { Write-Host "   registering $ns"; Invoke-Az provider register -n $ns --wait | Out-Null }
  else { Write-Host "   $ns registered" }
}

Step "Resource group $ResourceGroup ($Location)"
Invoke-Az group create -n $ResourceGroup -l $Location --tags project=clf-adapter purpose=happyrobot-demo -o none

Step "Image -> $Registry"
$registryId = Invoke-Az acr show -n $Registry --query id -o tsv
$loginServer = Invoke-Az acr show -n $Registry --query loginServer -o tsv
$version = (Select-String -Path (Join-Path $root "app\config.py") -Pattern 'VERSION = "([^"]+)"').Matches[0].Groups[1].Value
$image = "$loginServer/${Repository}:$version-$(Get-Date -Format yyyyMMddHHmm)"
Invoke-Docker build --quiet --target runtime -t $image $root | Out-Null
Invoke-Az acr login -n $Registry | Out-Null
Invoke-Docker push --quiet $image | Out-Null
Write-Host "   pushed $image"

Step "Managed identity $Identity (AcrPull on $Registry)"
$identityId = Invoke-Az identity list -g $ResourceGroup --query "[?name=='$Identity'].id" -o tsv
if (-not $identityId) { $identityId = Invoke-Az identity create -n $Identity -g $ResourceGroup -l $Location --query id -o tsv }
$principalId = Invoke-Az identity show --ids $identityId --query principalId -o tsv
# Filter by principal ID instead of --assignee: a new identity isn't in the directory (Graph) for a minute or two.
$assigned = Invoke-Az role assignment list --scope $registryId --query "[?principalId=='$principalId' && roleDefinitionName=='AcrPull'].id | [0]" -o tsv
if (-not $assigned) {
  Invoke-Az role assignment create --assignee-object-id $principalId --assignee-principal-type ServicePrincipal --role AcrPull --scope $registryId -o none
  Write-Host "   AcrPull granted; waiting 45 s for it to propagate"
  Start-Sleep -Seconds 45
} else { Write-Host "   AcrPull already granted" }

Step "Container Apps environment $Environment"
$envId = Invoke-Az containerapp env list -g $ResourceGroup --query "[?name=='$Environment'].id" -o tsv
if (-not $envId) {
  Write-Host "   creating (2-4 minutes)"
  Invoke-Az containerapp env create -n $Environment -g $ResourceGroup -l $Location -o none
  $envId = Invoke-Az containerapp env show -n $Environment -g $ResourceGroup --query id -o tsv
} else { Write-Host "   exists" }

Step "Secrets"
if (-not (Test-Path $SecretsFile)) {
  $content = @(
    "# Secrets for the Azure deployment of $App. Never commit this file.",
    "API_KEY=$(New-Secret)",
    "TMS_TOKEN=$(New-Secret 16)",
    "DEMO_CONSOLE_KEY=$(New-Secret 16)"
  ) -join "`r`n"
  [IO.File]::WriteAllText($SecretsFile, $content + "`r`n", (New-Object Text.UTF8Encoding $false))
  Write-Host "   generated new keys into $SecretsFile"
} else { Write-Host "   using existing $SecretsFile" }
$secrets = Read-DotEnv $SecretsFile

Step "Container app $App"
$exists = Invoke-Az containerapp list -g $ResourceGroup --query "[?name=='$App'].name" -o tsv
if (-not $exists) {
  $qaSecret = ""; $qaEnv = ""
  if ($secrets.QA_TEST_CARRIERS) {
    $qaSecret = "`n      - name: qa-test-carriers`n        value: '$($secrets.QA_TEST_CARRIERS)'"
    $qaEnv = "`n          - name: QA_TEST_CARRIERS`n            secretRef: qa-test-carriers"
  }
  $yaml = @"
location: $Location
identity:
  type: UserAssigned
  userAssignedIdentities:
    "$identityId": {}
properties:
  managedEnvironmentId: $envId
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 8080
      transport: auto
      allowInsecure: false
    registries:
      - server: $loginServer
        identity: $identityId
    secrets:
      - name: api-key
        value: '$($secrets.API_KEY)'
      - name: tms-token
        value: '$($secrets.TMS_TOKEN)'
      - name: demo-console-key
        value: '$($secrets.DEMO_CONSOLE_KEY)'$qaSecret
  template:
    containers:
      - name: clf-adapter
        image: $image
        resources:
          cpu: 0.25
          memory: 0.5Gi
        env:
          - name: ENV
            value: demo
          - name: API_KEY
            secretRef: api-key
          - name: TMS_TOKEN
            secretRef: tms-token
          - name: DEMO_CONSOLE_KEY
            secretRef: demo-console-key
          - name: DEMO_CARRIERS
            value: '$DemoCarriers'
          - name: TMS_FAULT_RATE
            value: '0'$qaEnv
        probes:
          - type: Startup
            httpGet:
              path: /healthz
              port: 8080
            periodSeconds: 3
            failureThreshold: 20
          - type: Liveness
            httpGet:
              path: /healthz
              port: 8080
            periodSeconds: 15
            failureThreshold: 3
          - type: Readiness
            httpGet:
              path: /healthz
              port: 8080
            periodSeconds: 10
    scale:
      minReplicas: 1
      maxReplicas: 1
"@
  $spec = Join-Path $env:TEMP ("clf-app-" + [guid]::NewGuid().ToString() + ".yaml")
  try {
    [IO.File]::WriteAllText($spec, $yaml, (New-Object Text.UTF8Encoding $false))
    Write-Host "   creating"
    Invoke-Az containerapp create -n $App -g $ResourceGroup --yaml $spec -o none
  } finally {
    Remove-Item $spec -ErrorAction SilentlyContinue  # it held the secrets
  }
} else {
  Write-Host "   rolling a new revision"
  $secretArgs = @("api-key=$($secrets.API_KEY)", "tms-token=$($secrets.TMS_TOKEN)", "demo-console-key=$($secrets.DEMO_CONSOLE_KEY)")
  $envArgs = @()
  if ($secrets.QA_TEST_CARRIERS) {  # optional: MC:code pairs with a fixed OTP for QA automation
    $secretArgs += "qa-test-carriers=$($secrets.QA_TEST_CARRIERS)"
    $envArgs = @("--set-env-vars", "QA_TEST_CARRIERS=secretref:qa-test-carriers")
  }
  Invoke-Az containerapp secret set -n $App -g $ResourceGroup --secrets @secretArgs -o none
  Invoke-Az containerapp update -n $App -g $ResourceGroup --image $image @envArgs -o none
}

$fqdn = Invoke-Az containerapp show -n $App -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv
$baseUrl = "https://$fqdn"
Set-DotEnvValue $SecretsFile "BASE_URL" $baseUrl

# The previous revision keeps answering /healthz during the rollout, so wait for the new revision
# to take all traffic, then for /v1/health to report the version just deployed.
$revision = Invoke-Az containerapp show -n $App -g $ResourceGroup --query properties.latestRevisionName -o tsv
Step "Waiting for revision $revision to take all traffic"
$live = $false
foreach ($attempt in 1..50) {
  $state = (Invoke-Az containerapp revision show -n $App -g $ResourceGroup --revision $revision --query "[properties.trafficWeight, properties.runningState]" -o tsv) -join " "
  if ($state -match '^100 Running') {
    try {
      $health = Invoke-RestMethod -Uri "$baseUrl/v1/health" -Headers @{ "X-API-Key" = $secrets.API_KEY } -TimeoutSec 10
      if ($health.version -eq $version -and $health.ok) { $live = $true; break }
    } catch { }
  }
  Start-Sleep -Seconds 6
}
if (-not $live) { throw "Revision $revision did not go live. Check: az containerapp logs show -n $App -g $ResourceGroup" }

Write-Host ""
Write-Host "Deployed $image" -ForegroundColor Green
Write-Host "  API      $baseUrl/v1"
Write-Host "  Docs     $baseUrl/docs"
Write-Host "  Console  $baseUrl/demo"
Write-Host "  Keys     $SecretsFile (API_KEY for HappyRobot, DEMO_CONSOLE_KEY for /demo)"
