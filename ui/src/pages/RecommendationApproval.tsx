import { useState } from "react";
import { api, ApiError } from "@/api/client";
import type { InfrastructureRecommendation } from "@/types";
import { describeCandidate, isNodeRow } from "@/utils/recommendations";
import { getIdentity } from "@/auth/session";

export default function RecommendationApproval() {
  const [investigationId, setInvestigationId] = useState("");
  const [recommendations, setRecommendations] = useState<InfrastructureRecommendation[]>([]);
  // Who approved a recommendation is an audit fact, so it comes from the
  // signed-in identity rather than a typed-in number. The backend rejects a
  // mismatch anyway (require_matching_employee_id), which used to make this
  // field a way to get a 403 rather than a way to approve as someone else.
  const identity = getIdentity();
  const reviewerId = identity?.employee_id ?? 0;
  const [comments, setComments] = useState<Record<number, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    if (!investigationId) return;
    setLoading(true);
    setError(null);
    setMessage(null);
    try {
      const result = await api.getInvestigationRecommendations(Number(investigationId));
      setRecommendations(result.recommendations);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function decide(recommendationId: number, action: "approve" | "reject") {
    setError(null);
    setMessage(null);
    try {
      const reason = comments[recommendationId];
      if (action === "approve") await api.approveRecommendation(recommendationId, reviewerId, reason);
      else await api.rejectRecommendation(recommendationId, reviewerId, reason);
      setMessage(`Recommendation #${recommendationId} ${action === "approve" ? "approved" : "rejected"}.`);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <>
      <h2>Recommendation Approval</h2>
      <p className="subtitle">Every recommendation requires a human decision with a recorded reviewer identity - the platform never applies a recommendation itself.</p>

      <div className="card">
        <div className="grid">
          <div className="form-row">
            <label>Investigation ID</label>
            <input value={investigationId} onChange={(e) => setInvestigationId(e.target.value)} />
          </div>
          <div className="form-row">
            <label>Reviewer</label>
            <input value={`${identity?.display_name ?? "—"} (${identity?.employee_number ?? "?"})`} readOnly />
          </div>
        </div>
        <button disabled={!investigationId || loading} onClick={load}>{loading ? "Loading..." : "Load recommendations"}</button>
      </div>

      {error && <div className="error-box">{error}</div>}
      {message && <div className="card" style={{ borderColor: "var(--green)" }}>{message}</div>}

      {recommendations.map((r) => (
        <div
          className="card"
          key={r.RecommendationId}
          style={isNodeRow(r) ? { marginLeft: 28 } : undefined}
        >
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <strong>
              #{r.RecommendationId} - {isNodeRow(r) ? "host" : "cluster"} rank {r.Rank} - {describeCandidate(r)}
            </strong>
            <span className="badge eligible">{r.Status}</span>
          </div>
          <p style={{ fontSize: 13 }}>{r.Explanation ?? "No explanation available."}</p>
          <div className="stat-label">Score {r.OverallScore ?? "—"} · Cost {r.EstimatedMonthlyCost != null ? `$${r.EstimatedMonthlyCost}` : "—"} · Headroom {r.ProjectedHeadroomPercent ?? "—"}%</div>
          <textarea
            placeholder="Comments (optional)"
            rows={2}
            style={{ width: "100%", marginTop: 8 }}
            value={comments[r.RecommendationId] ?? ""}
            onChange={(e) => setComments({ ...comments, [r.RecommendationId]: e.target.value })}
          />
          <div style={{ marginTop: 8 }}>
            <button onClick={() => decide(r.RecommendationId, "approve")}>Approve</button>{" "}
            <button className="danger" onClick={() => decide(r.RecommendationId, "reject")}>Reject</button>
          </div>
        </div>
      ))}
    </>
  );
}
