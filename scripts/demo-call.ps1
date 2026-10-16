<#
.SYNOPSIS
  Plays one HappyRobot call against the adapter, step by step, the way the voice workflow would.

.DESCRIPTION
  verify carrier -> send OTP -> (read code from the simulated phone) -> verify OTP ->
  search loads -> negotiate -> book -> hand off. Reads API_KEY, DEMO_CONSOLE_KEY and
  ADAPTER_PORT from ..\.env unless CLF_API_KEY / CLF_DEMO_CONSOLE_KEY are set.

.EXAMPLE
  .\scripts\demo-call.ps1
.EXAMPLE
  .\scripts\demo-call.ps1 -Mc 654321 -Origin "near Chicago" -Destination Texas -Offers "2700,2650,2600"
.EXAMPLE
  .\scripts\demo-call.ps1 -BaseUrl https://<your-app>.azurecontainerapps.io
#>
param(
  [string]$BaseUrl,
  [string]$Mc = "123456",
  [string]$Origin = "Joliet, IL",
  [string]$Destination = "Dallas, TX",
  [string]$Equipment = "dry_van",
  # Carrier's asks, comma-separated, e.g. "2600,2500". Empty = take the posted rate.
  [string]$Offers = "2600,2500",
  [string]$EnvFile = (Join-Path $PSScriptRoot "..\.env")
)
$ErrorActionPreference = "Stop"

function Read-DotEnv([string]$Path) {
  $vars = @{}
  if (Test-Path $Path) {
    foreach ($line in Get-Content $Path) {
      if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') { $vars[$Matches[1]] = $Matches[2] }
    }
  }
  return $vars
}

$dotenv = Read-DotEnv $EnvFile
$apiKey = if ($env:CLF_API_KEY) { $env:CLF_API_KEY } else { $dotenv["API_KEY"] }
$demoKey = if ($env:CLF_DEMO_CONSOLE_KEY) { $env:CLF_DEMO_CONSOLE_KEY } else { $dotenv["DEMO_CONSOLE_KEY"] }
if (-not $apiKey) { throw "No API key: set CLF_API_KEY or API_KEY in $EnvFile" }
if (-not $BaseUrl -and $dotenv["BASE_URL"]) { $BaseUrl = $dotenv["BASE_URL"] }  # e.g. -EnvFile .env.azure
if (-not $BaseUrl) {
  $port = if ($dotenv["ADAPTER_PORT"]) { $dotenv["ADAPTER_PORT"] } else { "8080" }
  $BaseUrl = "http://localhost:$port"
}
$BaseUrl = $BaseUrl.TrimEnd("/")
$callId = "demo-" + [guid]::NewGuid().ToString()
$headers = @{ "X-API-Key" = $apiKey; "X-Call-Id" = $callId }

function Invoke-Step([string]$Action, [string]$Path, [hashtable]$Body) {
  $json = $Body | ConvertTo-Json -Compress
  Write-Host ""
  Write-Host "-> $Action  POST /v1$Path  $json" -ForegroundColor Cyan
  try {
    $response = Invoke-RestMethod -Method Post -Uri "$BaseUrl/v1$Path" -Headers $headers -ContentType "application/json" -Body $json
  } catch {
    Write-Host "<- $($_.ErrorDetails.Message)" -ForegroundColor Red
    throw
  }
  $color = if ($response.ok -eq $false) { "Red" } else { "Green" }
  Write-Host ("<- " + ($response | ConvertTo-Json -Compress -Depth 6)) -ForegroundColor $color
  # Call-time failures come back as HTTP 200 with ok=false (HappyRobot can only read 2xx bodies).
  if ($response.ok -eq $false) { throw "$Action failed: $($response.error_code) - $($response.error_message)" }
  return $response
}

Write-Host "Call $callId against $BaseUrl" -ForegroundColor Yellow
$health = Invoke-RestMethod -Uri "$BaseUrl/v1/health" -Headers @{ "X-API-Key" = $apiKey }
Write-Host ("health: " + ($health | ConvertTo-Json -Compress))

$carrier = Invoke-Step "verify_carrier" "/carriers/verify" @{ mc_number = $Mc }
if (-not $carrier.authorized) { Write-Host "Carrier not authorized ($($carrier.reason)). Agent ends the call politely."; return }

$sent = Invoke-Step "send_otp" "/otp/send" @{ mc_number = $Mc }
if ($sent.status -ne "sent") { Write-Host "OTP not sent ($($sent.status)). Agent ends the call."; return }

if (-not $demoKey) { throw "No DEMO_CONSOLE_KEY: can't read the simulated phone" }
$inbox = Invoke-RestMethod -Uri "$BaseUrl/demo/api/phone?mc=$Mc" -Headers @{ "X-Demo-Key" = $demoKey }
$code = [regex]::Match($inbox.messages[0].body, '\b\d{6}\b').Value
Write-Host ""
Write-Host "   (carrier reads the SMS on their phone ending $($sent.masked_phone.Substring($sent.masked_phone.Length - 4)) and says the code)" -ForegroundColor DarkGray

$otp = Invoke-Step "verify_otp" "/otp/verify" @{ mc_number = $Mc; code = $code }
if ($otp.status -ne "verified") { Write-Host "OTP $($otp.status)."; return }

$search = @{ mc_number = $Mc; origin = $Origin; equipment_type = $Equipment }
if ($Destination) { $search.destination = $Destination }
$found = Invoke-Step "find_loads" "/loads/search" $search
if ($found.status -ne "found") { Write-Host "No loads match. Agent offers to call back."; return }
$load = $found.loads[0]
Write-Host ("   pitching {0}: {1} -> {2}, pickup {3}, {4} mi, posted `${5}" -f $load.load_id, $load.origin, $load.destination, $load.pickup_datetime, $load.miles, $load.loadboard_rate) -ForegroundColor DarkGray

$offerList = @($Offers -split '[,; ]+' | Where-Object { $_ } | ForEach-Object { [double]::Parse($_, [Globalization.CultureInfo]::InvariantCulture) })
$agreed = $load.loadboard_rate
$round = 0
foreach ($offer in $offerList) {
  $round++
  $decision = Invoke-Step "evaluate_offer" "/offers/evaluate" @{ load_id = $load.load_id; mc_number = $Mc; carrier_offer = $offer; round = $round }
  if ($decision.decision -eq "accept") { $agreed = $offer; break }
  if ($decision.decision -eq "final_decline") { Write-Host "No deal on this load. Agent thanks the carrier."; return }
  $agreed = $decision.counter_rate  # carrier takes our last counter if they stop pushing
}
if ($offerList.Count -gt 0) { Write-Host "   carrier agrees to `$$agreed" -ForegroundColor DarkGray }

$booking = Invoke-Step "book_load" "/loads/$($load.load_id)/book" @{ mc_number = $Mc; agreed_rate = $agreed }
if ($booking.status -ne "booked") { Write-Host "Booking $($booking.status)."; return }

$null = Invoke-Step "transfer_to_senior_rep" "/handoff" @{ load_id = $load.load_id; mc_number = $Mc; agreed_rate = $agreed }
Write-Host ""
Write-Host "Done: $($load.load_id) booked at `$$agreed ($($booking.booking_reference)). Watch it on $BaseUrl/demo" -ForegroundColor Yellow
