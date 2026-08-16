import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { CmdbApplication, InfrastructureCluster, CandidateScore, TradeOffSummary } from "@/types";

export default function RecommendationComparison() {
  const [applications, setApplications] = useState<CmdbApplication[]>([]);
  const [clusters, setClusters] = useState<InfrastructureCluster[]>([]);
  const [appCode, setAppCode] = useState("");
  const [selectedClusters, setSelectedClusters] = useState<string[]>([]);
  const [candidates, setCandidates] = useState<CandidateScore[] | null>(null);
  const [tradeoffs, setTradeoffs] = useState<TradeOffSummary | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getApplications().then(setApplications).catch(() => undefined);
    api.getClusters().then(setClusters).catch(() => undefined);
  }, []);

  async function compare() {
    if (!appCode) return;
    setError(null);
    try {
      const result = await api.getHostingRecommendations(appCode);
      setCandidates(result.candidates);
      // A fresh comparison invalidates the previous summary - leaving it on
      // screen would attribute one application's trade-offs to another.
      setTradeoffs(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function explain() {
    if (!appCode) return;
    setExplaining(true);
    setError(null);
    try {
      const result = await api.getHostingRecommendations(appCode, true);
      setCandidates(result.candidates);
      setTradeoffs(result.tradeoffs ?? null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setExplaining(false);
    }
  }

  const rows = selectedClusters
    .map((code) => candidates?.find((c) => c.cluster_code === code))
    .filter((c): c is CandidateScore => !!c);

  return (
    <>
      <h2>Recommendation Comparison</h2>
      <p className="subtitle">Compare specific candidate clusters side by side for an application.</p>

      <div className="card">
        <div className="grid">
          <div className="form-row">
            <label>Application</label>
            <select value={appCode} onChange={(e) => setAppCode(e.target.value)}>
              <option value="">Select...</option>
              {applications.map((a) => (
                <option key={a.applicationId} value={a.applicationCode}>{a.applicationCode}</option>
              ))}
            </select>
          </div>
          <div className="form-row">
            <label>Clusters to compare (ctrl/cmd-click for multiple)</label>
            <select multiple value={selectedClusters} onChange={(e) => setSelectedClusters(Array.from(e.target.selectedOptions, (o) => o.value))} style={{ minHeight: 100 }}>
              {clusters.map((c) => (
                <option key={c.clusterId} value={c.clusterCode}>{c.clusterCode}</option>
              ))}
            </select>
          </div>
        </div>
        <button disabled={!appCode} onClick={compare}>Compare</button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {rows.length > 0 && (
        <div className="card" style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                {rows.map((r) => <th key={r.cluster_code}>{r.cluster_code}</th>)}
              </tr>
            </thead>
            <tbody>
              <tr><td>Eligibility</td>{rows.map((r) => <td key={r.cluster_code}><span className={`badge ${r.eligibility_status === "Eligible" ? "eligible" : "rejected"}`}>{r.eligibility_status}</span></td>)}</tr>
              <tr><td>Overall score</td>{rows.map((r) => <td key={r.cluster_code}>{r.overall_score ?? "—"}</td>)}</tr>
              <tr><td>Est. monthly cost</td>{rows.map((r) => <td key={r.cluster_code}>{r.estimated_monthly_cost != null ? `$${r.estimated_monthly_cost}` : "—"}</td>)}</tr>
              <tr><td>Projected headroom %</td>{rows.map((r) => <td key={r.cluster_code}>{r.projected?.projected_headroom_percent ?? "—"}</td>)}</tr>
              <tr><td>Capacity score</td>{rows.map((r) => <td key={r.cluster_code}>{r.subscores?.capacity ?? "—"}</td>)}</tr>
              <tr><td>Compatibility score</td>{rows.map((r) => <td key={r.cluster_code}>{r.subscores?.compatibility ?? "—"}</td>)}</tr>
              <tr><td>Resiliency score</td>{rows.map((r) => <td key={r.cluster_code}>{r.subscores?.resiliency ?? "—"}</td>)}</tr>
              <tr><td>Cost score</td>{rows.map((r) => <td key={r.cluster_code}>{r.subscores?.cost ?? "—"}</td>)}</tr>
              <tr><td>Dependency score</td>{rows.map((r) => <td key={r.cluster_code}>{r.subscores?.dependency ?? "—"}</td>)}</tr>
              <tr><td>Risk score</td>{rows.map((r) => <td key={r.cluster_code}>{r.subscores?.risk ?? "—"}</td>)}</tr>
              <tr>
                <td>Rejection reasons</td>
                {rows.map((r) => (
                  <td key={r.cluster_code}>
                    {r.rule_results.filter((rule) => !rule.passed).map((rule) => (
                      <div key={rule.rule_id} className="rule-fail">{rule.rule_id}: {rule.reason}</div>
                    ))}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>

          {tradeoffs ? (
            <div style={{ marginTop: 14 }}>
              <strong>Trade-offs</strong>
              {tradeoffs.summary && <p style={{ marginTop: 6 }}>{tradeoffs.summary}</p>}
              {tradeoffs.key_differences && tradeoffs.key_differences.length > 0 && (
                <ul>
                  {tradeoffs.key_differences.map((d, i) => <li key={i}>{d}</li>)}
                </ul>
              )}
              {tradeoffs.recommendation && <div className="explain-box">{tradeoffs.recommendation}</div>}
            </div>
          ) : (
            <div style={{ marginTop: 14 }}>
              <button className="secondary" disabled={explaining} onClick={explain}>
                {explaining ? "Asking..." : "Summarise the trade-offs"}
              </button>
              <p className="subtitle" style={{ marginTop: 8, marginBottom: 0 }}>
                Compares the eligible candidates in prose. Every figure in the table above is
                computed and is unaffected by it.
              </p>
            </div>
          )}
        </div>
      )}
    </>
  );
}
