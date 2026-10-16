# Crystal Lane Freight · Carrier Sales Adapter (HappyRobot POC)

Middle service between the **HappyRobot voice agent** and Crystal Lane's systems: legacy TMS over
TCP, FMCSA and an SMS provider. It implements [`openapi.yaml`](openapi.yaml). All three external
systems are **simulated** for the POC, but they sit behind the same interfaces a real one would use.

The service enforces every business and compliance rule itself (call order, OTP limits, the
negotiation ceiling, approved booking rates). The agent's prompt is only a second line of defence.

```
HappyRobot workflow ──HTTPS + X-API-Key + X-Call-Id──▶  adapter (FastAPI, :8080)
                                                         ├─ FMCSA      → simulated authority lookup
                                                         ├─ SMS        → simulated handset (demo console)
                                                         └─ TMS client ─TCP, fixed-width──▶ tms-sim (:9100)
```

The TMS simulator is a real TCP server with fixed-width records and fault injection, so the spec's
`x-tms-integration` rules (1.5 s timeout, 3 attempts, 0.2/0.5 s backoff, 4 s budget, skip bad
records, idempotent booking) run for real rather than being mocked.

## Live demo

| What | Link |
|---|---|
| Voice agent (HappyRobot web call) | https://platform.happyrobot.ai/deployments/wd4xx0nj4szq |
| Operations dashboard (HappyRobot Apps) | https://slate-fjord-rmz94.custom-apps.happyrobot.ai/ |
| Adapter API docs (Azure Container Apps) | https://ca-clf-adapter.jollyglacier-d430eeb3.eastus.azurecontainerapps.io/docs |
| Executive report | [docs/Crystal-Lane-Freight-Executive-Report.pdf](docs/Crystal-Lane-Freight-Executive-Report.pdf) |
| Agent integration guide | [docs/HAPPYROBOT_INTEGRATION.md](docs/HAPPYROBOT_INTEGRATION.md) |

## Run it locally

Requires Docker Desktop.

```bash
copy .env.example .env
```

Edit `.env` and set long random values for `API_KEY`, `TMS_TOKEN` and `DEMO_CONSOLE_KEY` (on this
machine `.env` already exists with random values, and `ADAPTER_PORT=8180` because 8080 is taken).

```bash
docker compose up -d --build
```

| URL (port = `ADAPTER_PORT`) | What |
|---|---|
| `http://localhost:8180/v1/...` | The API (needs `X-API-Key`) |
| `http://localhost:8180/docs` | **API documentation** (Swagger UI) rendered from `openapi.yaml`. Public; *Authorize* with the API key to use *Try it out*. `/` redirects here |
| `http://localhost:8180/redoc` | The same contract in a reading layout |
| `http://localhost:8180/openapi.yaml` | The contract itself, with this deployment listed first in `servers` |
| `http://localhost:8180/openapi.json` | Spec generated from the code (useful to diff against the contract) |
| `http://localhost:8180/demo` | **Demo console**: simulated carrier phone (OTP SMS) + live call timeline. Asks for `DEMO_CONSOLE_KEY` |
| `http://localhost:8180/healthz` | Unauthenticated liveness probe |

Rehearse a full call (verify → OTP → search → negotiate → book → handoff) from PowerShell:

```bash
.\scripts\demo-call.ps1
```

```bash
.\scripts\demo-call.ps1 -Mc 654321 -Origin "near Chicago" -Destination Texas -Offers "2700,2650,2600"
```

Stop with `docker compose down`. Restart from a clean board with the console's **Reset demo**
button (clears calls, SMS and TMS bookings).

## Demo cheat sheet

**Carriers (simulated FMCSA)**

| MC | Result |
|---|---|
| `123456` | Blue Ridge Transport LLC, active, phone on file ends **4821** |
| `654321` | Lone Star Haulers Inc, active, phone ends 6543 |
| `246810` | Great Lakes Carriers Co, active but **no phone on file** → `no_phone_on_file` |
| `111111` | Midwest Freightways LLC, **inactive** |
| `222222` | Rapid Route Logistics Inc, **revoked** |
| `999999` | FMCSA outage → `ok: false`, `fmcsa_unavailable` (retryable) |
| `900001` | QA Test Carrier LLC: active, phone ends 0001, **OTP is always `112233`** (from `QA_TEST_CARRIERS`, for QA automation) |
| anything else | `not_found` |

Any MC added to `DEMO_CARRIERS` (`MC:+phone`) becomes an active carrier with that phone on file.

**QA automation.** `QA_TEST_CARRIERS` (`MC:code` pairs) gives test carriers a fixed OTP. The flow is otherwise identical: the code must be sent first, and the 3 attempts, 5-minute expiry and resend limit still apply. For random codes, a test can read the code sent on its own call from `GET /demo/api/phone?call_id=<X-Call-Id>` (header `X-Demo-Key`): the `code` field of `messages[0]`. Demo mode only; never put the fixed code in the agent's prompt or documentation.

**Loads (simulated TMS).** 20 loads; pickup dates are generated relative to today, so the board
never goes stale. The main lane is **Joliet, IL → Dallas, TX, dry van**:

| Load | Lane | Posted |
|---|---|---|
| LD-10482 | Joliet, IL → Dallas, TX · dry van · 925 mi | $2,100 |
| LD-10542 | Chicago, IL → Dallas, TX · dry van | $2,200 |
| LD-10539 | Joliet, IL → Dallas, TX · reefer | $2,450 |
| LD-10499 | Joliet, IL → Dallas, TX · already covered (never returned) | – |

Others cover Atlanta, Dallas/Fort Worth, LA/Ontario, Newark, Columbus, Denver and Kansas City.
Search understands spoken places: "near Chicago", "DFW", "Texas", "Saint Louis".

**Negotiation on LD-10482** (posted $2,100). An ask above the ceiling gets counters of
$2,200 → $2,300 → $2,400 (final), then `final_decline`. An ask of $2,300 in round 1 gets a
meet-in-the-middle counter of $2,200; any ask ≤ $2,100 is accepted.

**Fault injection.** Set `TMS_FAULT_RATE=0.3` in `.env` and run `docker compose up -d`: the
adapter's logs show `tms_attempt_failed` and `tms_records_skipped` while calls still succeed.
About 1 in 100 TMS calls still fails after all 3 attempts and returns `ok: false`, `tms_unavailable` (retryable). Keep it
at `0` for the live demo unless you want to show that.

## HappyRobot tool setup

The complete, self-contained guide to hand to HappyRobot is
[`docs/HAPPYROBOT_INTEGRATION.md`](docs/HAPPYROBOT_INTEGRATION.md): connection, response contract, every
endpoint with its outcomes and agent actions, tool definitions, an example call, test data and the
OpenAPI spec (without the internal negotiation policy). Summary:

Every tool is an HTTPS POST webhook with two headers:

- `X-API-Key`: the `API_KEY` secret (store it as a secret in HappyRobot, never in the prompt)
- `X-Call-Id`: the workflow's run/call ID variable, so all requests of one call share state

| Agent action | Endpoint | Body |
|---|---|---|
| `verify_carrier` | `POST /v1/carriers/verify` | `mc_number` |
| `send_otp` | `POST /v1/otp/send` | `mc_number` |
| `verify_otp` | `POST /v1/otp/verify` | `mc_number`, `code` |
| `find_loads` | `POST /v1/loads/search` | `mc_number`, `origin`, `equipment_type`, optional `destination`, `pickup_date` (YYYY-MM-DD) |
| `evaluate_offer` | `POST /v1/offers/evaluate` | `load_id`, `mc_number`, `carrier_offer` (USD), `round` |
| `book_load` | `POST /v1/loads/{load_id}/book` | `mc_number`, `agreed_rate` |
| `transfer_to_senior_rep` | `POST /v1/handoff` | `load_id`, `mc_number`, `agreed_rate` |

**One response shape per endpoint, always HTTP 200.** HappyRobot reads responses with an LLM and
turns any non-2xx reply into one opaque text, so every endpoint answers HTTP 200 with the same keys,
success or failure. Every reply starts with `ok`:

```json
{ "ok": true, "error_code": null, "error_message": null, "retryable": false, "authorized": true, "carrier_name": "Blue Ridge Transport LLC", "authority_status": "active", "reason": null }
{ "ok": true, "error_code": null, "error_message": null, "retryable": false, "authorized": false, "carrier_name": null, "authority_status": "revoked", "reason": "authority_revoked" }
{ "ok": false, "error_code": "invalid_request", "error_message": "mc_number is required", "retryable": false, "authorized": null, "carrier_name": null, "authority_status": null, "reason": null }
```

How the agent should read results:

- **`ok: true`**: the request was processed. The answer is in `status` / `decision` / `authorized`,
  and it may be a "no" (carrier revoked, no match, load unavailable). Speak to it.
- **`ok: false`**: the request itself failed. Every other field is `null` (lists are `[]`); `null` means unknown, never "no".
  - `invalid_request`: `error_message` says what to fix (for example "mc_number is required").
  - `step_out_of_order`: a previous step is missing; `error_message` names it.
  - `retryable: true` (`tms_unavailable`, `fmcsa_unavailable`, `rate_limited`...): retry once, then apologise and offer a callback.
- **The only non-200 reply is 401** (missing or wrong API key): a setup problem, not a call outcome.
- Field names avoid `error`, which HappyRobot uses itself for failed requests.
- Logs keep the HTTP-equivalent status of each failure as `error_status` (400, 409, 429, 503...) for monitoring.
- Money comes back as whole dollars (`2250`, not `2250.0`) so it reads naturally.

The service also accepts voice-style input: `"123 456"` for an MC, `"482 913"` for a code,
`"Dry Van"` for equipment, `"$2,250"` for an offer, `"ld-10482"` for a load ID.

## Rules the service enforces

- **Order per X-Call-Id**: verify → send OTP → verify OTP → search → evaluate → book → handoff (`step_out_of_order` otherwise).
  You can only negotiate on or book loads that were returned to *this* call.
- **OTP**: sent only to the number on file (there is no phone field), 6 digits from `secrets`, stored
  as an HMAC, compared in constant time, 300 s expiry, 2 sends per call (a resend invalidates the
  previous code), 3 attempts then locked. The code never appears in a response or a log.
- **Margin**: `max_rate` is removed at the single point where TMS loads become API objects, and the
  response models forbid extra fields. Rounds are counted by the service. Counters never exceed the
  ceiling or the carrier's ask, and always go up. Booking accepts only the posted rate or a rate approved
  on this call. Booking is idempotent and carries an idempotency key to the TMS, so retries can't double-book.
- **Security**: API key compared in constant time; two comma-separated keys allow zero-downtime rotation;
  60 requests/minute per call (`rate_limited`); call state expires after 1 h; JSON logs carry only explicit fields
  (no bodies, no headers); `Cache-Control: no-store`; non-root, read-only containers.
  The demo console uses its own key, so the agent's API key can never read an OTP.

## Tests

```bash
docker build --target test -t clf-adapter:test .
```

```bash
docker run --rm clf-adapter:test
```

95 tests cover the flow, step order, OTP limits and expiry, the negotiation policy and its
invariants, booking idempotency and double-booking, TMS timeouts, retries and malformed records,
auth, rate limiting, and a scan proving the OTP code, phone, keys and `max_rate` never reach
responses or logs.

## Changes to the draft spec

Recorded in `openapi.yaml`:

1. **Response envelope, always HTTP 200** (contract 1.2.0): every reply starts with `ok`, `error_code`,
   `error_message`, `retryable`; failures are 200 with `ok: false`. Only a wrong API key is 401.
   The draft's 400/409/503 replies were unreadable for HappyRobot, which collapses non-2xx bodies into one text.
2. **`rate_limited`** added to the error codes (the spec asks for a rate limit but had no code for it).
3. **`round` has no maximum.** The service counts rounds itself, so a 4th attempt gets `final_decline`.
4. **`/carriers/verify` fails with `step_out_of_order`** if the call already passed OTP for a different MC (no identity switching mid-call).
5. **`GET /healthz`** (unauthenticated, outside `/v1`) for container probes; `/v1/health` stays behind the API key.
6. Demo-only `/demo` console (not part of the contract, off without `DEMO_CONSOLE_KEY`).

## Configuration

| Variable | Purpose |
|---|---|
| `ENV` | `demo` (default). `production` is refused at startup because the integrations are simulated |
| `API_KEY` | Agent's shared secret, ≥16 chars. `key1,key2` during rotation |
| `TMS_HOST`, `TMS_PORT`, `TMS_TOKEN` | Legacy TMS. **Leave `TMS_HOST` empty to run the simulator inside the adapter** (single container) |
| `TMS_EMBEDDED_SIM` | Force embedded simulator on/off (default: on when `TMS_HOST` is empty) |
| `DEMO_CARRIERS` | `MC:+phone` pairs on file |
| `TMS_FAULT_RATE` | 0–1 probability of a simulated TMS fault |
| `DEMO_CONSOLE_KEY` | Enables `/demo`; must differ from `API_KEY` |
| `QA_TEST_CARRIERS` | QA automation: `MC:6-digit-code` pairs with a fixed OTP. Demo only; refused in production |
| `ADAPTER_PORT` | Host port for docker compose (default 8080) |
| `FMCSA_KEY`, `SMS_PROVIDER_KEY` | Placeholders for the real integrations (unused in the POC) |

## Azure deployment

Runs on **Azure Container Apps** as a single container (the TMS simulator runs in-process because
`TMS_HOST` is unset). Deploy or redeploy with one command (Azure CLI + Docker Desktop, signed in with `az login`):

```bash
.\scripts\deploy-azure.ps1
```

| Resource | Name | Notes |
|---|---|---|
| Resource group | `rg-clf-adapter-demo` (East US) | Holds everything below except the image |
| Container Apps environment | `cae-clf-adapter-demo` | Consumption plan: no fixed cost. Logs go to its own Log Analytics workspace |
| Container app | `ca-clf-adapter` | External HTTPS ingress (HTTP disabled), 0.25 vCPU / 0.5 GiB, **exactly 1 replica** because call state is in memory |
| Managed identity | `id-clf-adapter` | `AcrPull` on the registry; no registry passwords stored in the app |
| Image | `<registry>.azurecr.io/clf-adapter:<version>-<timestamp>` | An existing Azure Container Registry, set as `AZURE_REGISTRY` in `.env.azure` (with `AZURE_SUBSCRIPTION`) |

- **Secrets** are generated on the first run into `.env.azure` (git- and docker-ignored, never printed):
  `API_KEY` (give it to HappyRobot), `DEMO_CONSOLE_KEY` (for `/demo`), `TMS_TOKEN`, plus `BASE_URL`.
  They are stored as Container App secrets. Delete the file and re-run to rotate.
- **Re-running** builds a new image tag, pushes it and rolls a new revision.
- **Rehearse against Azure:** `.\scripts\demo-call.ps1 -EnvFile .env.azure`
- **Logs:** `az containerapp logs show -n ca-clf-adapter -g rg-clf-adapter-demo --follow`
- **Keep min replicas at 1** (about US$6/month). Scaling to zero makes the first webhook wait for a
  cold start, which breaks the HappyRobot call. One replica is also required because call state is in memory.
- **Why was a request rejected?** Every failed request's log line carries `error_code`, `error_message` and `error_status` (never the submitted values):
  `az containerapp logs show -n ca-clf-adapter -g rg-clf-adapter-demo --follow --format text`
- **Tear down:** `az group delete -n rg-clf-adapter-demo --yes` and
  `az acr repository delete -n <registry> --repository clf-adapter --yes`.
