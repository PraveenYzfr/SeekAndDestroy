# SeekAndDestroy — Demo Scenarios

All scenarios below were run against the live seeded database while building this platform - the output shown is real, not illustrative. Run `mcp-client\interactive_client.py --demo` to reproduce all ten in one pass.

## 1. Finding hosting space for an application

```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Find the best clusters for hosting APP-PAYMENTS."
```
`APP-PAYMENTS` is deliberately seeded as a high-growth (40%/year) Critical/Restricted application - the demo shows **zero eligible candidates**, each with a specific rule failure (capacity, headroom, or resiliency depending on the cluster). This is not a bug: it's the platform correctly identifying that current infrastructure cannot absorb the projected growth, which is exactly why `APP-PAYMENTS` has an open `CapacityRequest` in the seed data (Scenario B follow-up).

## 2. Comparing candidate clusters

```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Compare dal-03 and den-03 for APP-CRM."
```
`dal-03` is rejected (RULE-002: VMware cluster, Kubernetes-only application); `den-03` is evaluated on its actual merits. The comparison view in the UI (`Recommendation Comparison` screen) shows this side by side with every sub-score.

## 3. Cluster right-sizing

```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Which clusters require right-sizing?"
```
Surfaces the 3 seeded overprovisioned clusters (`nyc-03`, `dal-03`, `den-07`) with real node-reduction recommendations (e.g. `nyc-03`: 6 → 3 nodes) and the 2 nearing-capacity clusters, each with a savings figure computed from the actual per-node cost.

## 4. Application allocation right-sizing

```bash
curl -X POST http://127.0.0.1:8088/api/right-sizing/applications -d "{\"application_code\": \"APP-NOTIFICATIONS\"}"
```
Compares `ApplicationHosting.Allocated*` against measured `ApplicationUsage` and classifies `OverAllocated`/`UnderAllocated`/`RightSized` with a dollar estimate.

## 5. Workload consolidation

```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Which applications can be safely consolidated?"
```
`APP-NOTIFICATIONS` (on overprovisioned `nyc-03`) consolidates onto `atl-03`, an already-utilized, fully-eligible cluster, with a real computed monthly saving (~$1,162/mo in the seed data) - not a placement, a genuine consolidation (target already hosts other workloads).

## 6. Capacity forecasting

```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Forecast capacity for clt-03 for the next 90 days."
```
`clt-03` is seeded with a rising CPU trend; the OLS forecaster correctly predicts a threshold breach (>75%) within the 90-day horizon with a specific exhaustion date and confidence band. `nyc-03` (overprovisioned), by contrast, shows no breach.

## 7. Human approval workflow

```python
from app.graph.graph import run_investigation, resume_investigation
result = run_investigation(query="Find the best clusters for hosting APP-ONBOARDING.", created_by=1)
# result["status"] == "AwaitingReview" - the graph is genuinely paused, checkpointed to SQLite
resume_investigation(investigation_id=result["investigation_id"], decision="Approve", reviewer_employee_id=1, comments="ok")
# now "Completed" - recommendations persisted with EvidenceJson on every row
```
Or via the UI: `Investigation Detail` screen to start/inspect, `Recommendation Approval` screen to approve/reject with a recorded reviewer identity.

## 8. Why was a candidate rejected

```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Why was clt-03 rejected?"
```
Prints every failing rule with its exact reason (e.g. `RULE-004: Cluster tier 'Tier-2' does not meet required tier 'Tier-1'`).

## 9. New capacity requirement (Scenario B)

```bash
curl -X POST http://127.0.0.1:8088/api/capacity/recommendations -d "{\"environment\":\"Production\",\"cpuCores\":16,\"memoryGb\":64,\"storageGb\":2000,\"platform\":\"Kubernetes\",\"availabilityTier\":\"Tier-1\",\"dataClassification\":\"Confidential\",\"requestedByEmployeeId\":1}"
```
Creates a `CapacityRequest` row and returns ranked candidates for a workload with no existing application record at all.

## 10. Generating a hosting recommendation report

```bash
.venv\Scripts\python.exe mcp-client\interactive_client.py --query "Generate a hosting recommendation report."
```
For a full narrative report (executive summary, top recommendation, alternatives, risks, next steps, required human action), run an investigation end to end via `run_investigation` / `resume_investigation` as in scenario 7 and inspect `final_report`.

---

## Engineered seed scenarios (for reference)

`scripts/generate_seed.py::SCENARIOS` is the single source of truth:

| Scenario | Codes |
|---|---|
| Overprovisioned clusters | `nyc-03`, `dal-03`, `den-07` |
| Nearing CPU capacity | `clt-03`, `phx-05` |
| Nearing memory capacity | `clt-13`, `phx-03` |
| High-cost, low utilization | `msp-03`, `cmh-03` |
| Suitable for new workloads | `atl-03`, `den-03`, `nyc-05` |
| Insufficient resiliency | `phx-03`, `cmh-03` |
| Compliance mismatch (existing hosting) | `clt-13` (hosts `APP-PAYROLL`), `msp-03` (hosts `APP-BILLING`) |
| Forecast exhaustion within 90 days | `clt-03` (CPU), `clt-13` (memory), `cmh-03` (storage) |
| Applications on poor-fit infrastructure | `APP-CHATBOT`, `APP-BATCHSCHED`, `APP-SUPPORTDESK`, `APP-INVENTORY`, `APP-LEGACYMF` |
| Consolidation candidates | `APP-NOTIFICATIONS`, `APP-DOCSTORE`, `APP-SEARCH`, `APP-CACHEADMIN` |
| Applications needing expansion | `APP-PAYMENTS`, `APP-LEDGER`, `APP-MLSCORING` |
| Applications with strong alternatives | `APP-CRM`, `APP-ANALYTICS`, `APP-ETL`, `APP-RISKENGINE` |

`APP-FRAUD` → `APP-IDENTITY` is the RULE-008 fixture: a critical, high-latency-sensitivity dependency crossing regions (Mumbai ↔ Chennai).
