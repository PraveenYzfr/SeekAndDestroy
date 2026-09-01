import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { InfrastructureCluster, ClusterRightSizingResult } from "@/types";

export default function Dashboard() {
  const [clusters, setClusters] = useState<InfrastructureCluster[]>([]);
  const [rightSizing, setRightSizing] = useState<ClusterRightSizingResult[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.getClusters(), api.getClusterRightSizing()])
      .then(([clusterList, sizing]) => {
        setClusters(clusterList);
        setRightSizing(sizing.results);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>Loading dashboard...</p>;
  if (error) return <div className="error-box">{error}</div>;

  const overprovisioned = rightSizing.filter((r) => r.classification === "Overprovisioned");
  const underprovisioned = rightSizing.filter((r) => r.classification === "Underprovisioned");
  // Nodes, not currency. This platform answers capacity questions; a dollar
  // figure invites the reader to compare options on cost, which is not what any
  // of the scoring here optimises for. node_delta is the same finding expressed
  // in the unit the recommendation is actually made in.
  const reclaimableNodes = overprovisioned.reduce((sum, r) => sum + Math.max(0, -r.node_delta), 0);
  const avgCpu = rightSizing.length
    ? (rightSizing.reduce((s, r) => s + r.snapshot.current_cpu_utilization_percent, 0) / rightSizing.length).toFixed(1)
    : "—";
  const avgMem = rightSizing.length
    ? (rightSizing.reduce((s, r) => s + r.snapshot.current_memory_utilization_percent, 0) / rightSizing.length).toFixed(1)
    : "—";

  return (
    <>
      <h2>Infrastructure Dashboard</h2>
      <p className="subtitle">Fleet-wide capacity, utilization and right-sizing at a glance.</p>

      <div className="grid">
        <div className="card">
          <div className="stat-label">Total clusters</div>
          <div className="stat">{clusters.length}</div>
        </div>
        <div className="card">
          <div className="stat-label">Avg CPU utilization</div>
          <div className="stat">{avgCpu}%</div>
        </div>
        <div className="card">
          <div className="stat-label">Avg memory utilization</div>
          <div className="stat">{avgMem}%</div>
        </div>
        <div className="card">
          <div className="stat-label">Overprovisioned clusters</div>
          <div className="stat">{overprovisioned.length}</div>
        </div>
        <div className="card">
          <div className="stat-label">Clusters nearing capacity</div>
          <div className="stat">{underprovisioned.length}</div>
        </div>
        <div className="card">
          <div className="stat-label">Reclaimable nodes</div>
          <div className="stat">{reclaimableNodes}</div>
        </div>
      </div>

      <div className="card">
        <strong>Right-sizing opportunities</strong>
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th>Cluster</th>
              <th>Classification</th>
              <th>CPU %</th>
              <th>Memory %</th>
              <th>Nodes</th>
              <th>Recommended</th>
              <th>Node change</th>
            </tr>
          </thead>
          <tbody>
            {rightSizing
              .filter((r) => r.classification !== "Healthy")
              .map((r) => (
                <tr key={r.cluster_id}>
                  <td>{r.cluster_code}</td>
                  <td>
                    <span className={`badge ${r.classification.toLowerCase()}`}>{r.classification}</span>
                  </td>
                  <td>{r.snapshot.current_cpu_utilization_percent}%</td>
                  <td>{r.snapshot.current_memory_utilization_percent}%</td>
                  <td>{r.current_node_count}</td>
                  <td>{r.recommended_node_count}</td>
                  <td>{r.node_delta > 0 ? `+${r.node_delta}` : r.node_delta}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
