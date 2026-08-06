import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { ClusterRightSizingResult } from "@/types";

export default function ClusterRightSizing() {
  const [results, setResults] = useState<ClusterRightSizingResult[]>([]);
  const [filter, setFilter] = useState<string>("All");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .getClusterRightSizing()
      .then((r) => setResults(r.results))
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const visible = filter === "All" ? results : results.filter((r) => r.classification === filter);

  return (
    <>
      <h2>Cluster Right-Sizing</h2>
      <p className="subtitle">Current utilization, recommended allocation, savings and risks for every cluster.</p>

      <div className="card">
        <div className="form-row" style={{ maxWidth: 240 }}>
          <label>Filter by classification</label>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option>All</option>
            <option>Overprovisioned</option>
            <option>Underprovisioned</option>
            <option>Healthy</option>
          </select>
        </div>
      </div>

      {loading && <p>Loading...</p>}
      {error && <div className="error-box">{error}</div>}

      {visible.map((r) => (
        <div className="card" key={r.cluster_id}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>{r.cluster_code}</strong>
            <span className={`badge ${r.classification.toLowerCase()}`}>{r.classification}</span>
          </div>
          <div className="grid" style={{ marginTop: 10 }}>
            <div>
              <div className="stat-label">CPU utilization</div>
              <div>{r.snapshot.current_cpu_utilization_percent}%</div>
            </div>
            <div>
              <div className="stat-label">Memory utilization</div>
              <div>{r.snapshot.current_memory_utilization_percent}%</div>
            </div>
            <div>
              <div className="stat-label">Nodes (current → recommended)</div>
              <div>{r.current_node_count} → {r.recommended_node_count}</div>
            </div>
            <div>
              <div className="stat-label">Monthly savings</div>
              <div>${r.estimated_monthly_savings.toLocaleString()}</div>
            </div>
            <div>
              <div className="stat-label">Annual savings</div>
              <div>${r.estimated_annual_savings.toLocaleString()}</div>
            </div>
          </div>
          <p style={{ marginTop: 10, fontSize: 13 }}>{r.rationale}</p>
          {r.risks.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {r.risks.map((risk, i) => <li key={i} className="rule-fail">{risk}</li>)}
            </ul>
          )}
        </div>
      ))}
    </>
  );
}
