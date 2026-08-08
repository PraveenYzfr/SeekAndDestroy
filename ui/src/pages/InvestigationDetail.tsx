import { useState } from "react";
import { api, ApiError } from "@/api/client";
import type { Investigation, InfrastructureRecommendation, RunInvestigationResult } from "@/types";
import { describeCandidate, isNodeRow } from "@/utils/recommendations";

const GRAPH_STAGES = [
  "parse_user_request", "load_application_requirements", "create_investigation_plan",
  "identify_candidate_infrastructure", "apply_hard_eligibility_rules", "calculate_current_capacity",
  "calculate_projected_utilization", "run_capacity_forecast", "analyze_dependencies",
  "calculate_candidate_scores", "rank_candidates", "select_candidate_nodes", "retrieve_related_context",
  "generate_recommendation_explanations", "assess_risk_and_confidence", "human_review_interrupt",
  "generate_final_report", "persist_recommendations", "complete_investigation",
];

export default function InvestigationDetail() {
  const [query, setQuery] = useState("Find the best clusters for hosting APP-PAYMENTS.");
  const [runResult, setRunResult] = useState<RunInvestigationResult | null>(null);
  const [investigationId, setInvestigationId] = useState<string>("");
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [recommendations, setRecommendations] = useState<InfrastructureRecommendation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function startInvestigation() {
    setLoading(true);
    setError(null);
    try {
      const result = await api.createInvestigation(query, 1);
      setRunResult(result);
      setInvestigationId(String(result.investigation_id));
      await loadInvestigation(result.investigation_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function loadInvestigation(id: number) {
    const inv = await api.getInvestigation(id);
    setInvestigation(inv);
    const recs = await api.getInvestigationRecommendations(id);
    setRecommendations(recs.recommendations);
  }

  async function lookup() {
    if (!investigationId) return;
    setLoading(true);
    setError(null);
    try {
      await loadInvestigation(Number(investigationId));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <h2>Investigation Detail</h2>
      <p className="subtitle">Run a new investigation through the LangGraph workflow, or inspect an existing one.</p>

      <div className="card">
        <div className="form-row">
          <label>Natural-language request</label>
          <textarea rows={2} value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <button disabled={loading} onClick={startInvestigation}>{loading ? "Running..." : "Start investigation"}</button>
        {" "}
        <input placeholder="or enter an investigation id" value={investigationId} onChange={(e) => setInvestigationId(e.target.value)} style={{ width: 200, marginLeft: 12 }} />
        <button className="secondary" disabled={loading || !investigationId} onClick={lookup}>Load</button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {investigation && (
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <strong>Investigation #{investigation.InvestigationId}</strong>
            <span className="badge eligible">{investigation.Status}</span>
          </div>
          <p style={{ fontSize: 13 }}>{investigation.Query}</p>
          <div className="stat-label">Type: {investigation.InvestigationType}</div>
        </div>
      )}

      <div className="card">
        <strong>LangGraph stages</strong>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>
          {GRAPH_STAGES.map((stage) => (
            <span key={stage} className="badge healthy" style={{ fontWeight: 400 }}>{stage}</span>
          ))}
        </div>
      </div>

      {runResult?.review_payload && (
        <div className="card">
          <strong>Awaiting human review</strong>
          <p style={{ fontSize: 13 }}>{runResult.review_payload.message}</p>
          <div>Top candidates: {runResult.review_payload.top_candidates.join(", ")}</div>
          {runResult.review_payload.top_hosts_by_cluster && (
            <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 13 }}>
              {Object.entries(runResult.review_payload.top_hosts_by_cluster).map(([cluster, hosts]) => (
                <li key={cluster}>
                  {cluster}: {hosts.length > 0 ? hosts.join(", ") : "no eligible hosts"}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {recommendations.length > 0 && (
        <div className="card">
          <strong>Recommendations</strong>
          <table style={{ marginTop: 10 }}>
            <thead>
              <tr><th>Rank</th><th>Candidate</th><th>Status</th><th>Score</th><th>Cost</th><th>Approval status</th></tr>
            </thead>
            <tbody>
              {recommendations.map((r) => (
                <tr key={r.RecommendationId}>
                  <td style={isNodeRow(r) ? { paddingLeft: 20, opacity: 0.75 } : undefined}>{r.Rank}</td>
                  <td style={isNodeRow(r) ? { paddingLeft: 20, opacity: 0.85 } : undefined}>
                    {isNodeRow(r) ? "↳ " : ""}
                    {describeCandidate(r)}
                  </td>
                  <td><span className={`badge ${r.EligibilityStatus === "Eligible" ? "eligible" : "rejected"}`}>{r.EligibilityStatus}</span></td>
                  <td>{r.OverallScore ?? "—"}</td>
                  <td>{r.EstimatedMonthlyCost != null ? `$${r.EstimatedMonthlyCost}` : "—"}</td>
                  <td>{r.Status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
