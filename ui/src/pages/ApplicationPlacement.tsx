import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { CmdbApplication, CandidateScore } from "@/types";

export default function ApplicationPlacement() {
  const [applications, setApplications] = useState<CmdbApplication[]>([]);
  const [appCode, setAppCode] = useState("");
  const [candidates, setCandidates] = useState<CandidateScore[] | null>(null);
  const [currentCluster, setCurrentCluster] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.getApplications().then(setApplications).catch(() => undefined);
  }, []);

  async function analyze() {
    if (!appCode) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getHostingRecommendations(appCode);
      setCandidates(result.candidates);
      setCurrentCluster(result.application);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const alternatives = (candidates ?? []).filter((c) => c.eligibility_status === "Eligible").slice(0, 5);
  const bestCost = alternatives.length ? Math.min(...alternatives.map((c) => c.estimated_monthly_cost ?? Infinity)) : null;

  return (
    <>
      <h2>Application Placement</h2>
      <p className="subtitle">Current hosting vs. alternative candidates, with migration benefits and dependency considerations.</p>

      <div className="card">
        <div className="form-row" style={{ maxWidth: 320 }}>
          <label>Application</label>
          <select value={appCode} onChange={(e) => setAppCode(e.target.value)}>
            <option value="">Select an application...</option>
            {applications.map((a) => (
              <option key={a.applicationId} value={a.applicationCode}>{a.applicationCode} - {a.applicationName}</option>
            ))}
          </select>
        </div>
        <button disabled={!appCode || loading} onClick={analyze}>{loading ? "Analyzing..." : "Analyze placement"}</button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {currentCluster && (
        <div className="card">
          <strong>Application requirement</strong>
          <pre style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>{JSON.stringify(currentCluster, null, 2)}</pre>
        </div>
      )}

      {alternatives.length > 0 && (
        <div className="card">
          <strong>Top eligible alternatives</strong>
          <table style={{ marginTop: 10 }}>
            <thead>
              <tr><th>Cluster</th><th>Score</th><th>Monthly Cost</th><th>Headroom %</th><th></th></tr>
            </thead>
            <tbody>
              {alternatives.map((c) => (
                <tr key={c.cluster_code}>
                  <td>{c.cluster_code}</td>
                  <td>{c.overall_score}</td>
                  <td>
                    ${c.estimated_monthly_cost?.toLocaleString()}
                    {bestCost != null && c.estimated_monthly_cost === bestCost && (
                      <span style={{ color: "var(--green)", marginLeft: 6, fontSize: 11 }}>lowest cost</span>
                    )}
                  </td>
                  <td>{c.projected?.projected_headroom_percent}%</td>
                  <td>
                    <button className="secondary" disabled>Request migration (out of scope - recommendations only)</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
