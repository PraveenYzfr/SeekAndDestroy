import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { InfrastructureCluster, ClusterForecast, ResourceForecast } from "@/types";

function ForecastRow({ label, forecast }: { label: string; forecast: ResourceForecast }) {
  return (
    <div className="card">
      <strong>{label}</strong>
      <div className="grid" style={{ marginTop: 8 }}>
        <div><div className="stat-label">Current</div><div>{forecast.current_percent}%</div></div>
        <div><div className="stat-label">Predicted ({forecast.horizon_days}d)</div><div>{forecast.predicted_percent}%</div></div>
        <div><div className="stat-label">Confidence range</div><div>{forecast.confidence_low_percent}% - {forecast.confidence_high_percent}%</div></div>
        <div><div className="stat-label">Threshold crossing</div><div>{forecast.exhaustion_date ?? "Not projected"}</div></div>
      </div>
      <p style={{ marginTop: 8, fontSize: 13 }} className={forecast.breaches_threshold_within_horizon ? "rule-fail" : "rule-pass"}>
        {forecast.recommended_action}
      </p>
    </div>
  );
}

export default function CapacityForecast() {
  const [clusters, setClusters] = useState<InfrastructureCluster[]>([]);
  const [clusterCode, setClusterCode] = useState("");
  const [horizon, setHorizon] = useState(90);
  const [forecast, setForecast] = useState<ClusterForecast | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.getClusters().then(setClusters).catch(() => undefined);
  }, []);

  async function run() {
    if (!clusterCode) return;
    setLoading(true);
    setError(null);
    try {
      setForecast(await api.getForecast(clusterCode, horizon));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <h2>Capacity Forecast</h2>
      <p className="subtitle">Deterministic trend forecast for CPU, memory and storage.</p>

      <div className="card">
        <div className="grid">
          <div className="form-row">
            <label>Cluster</label>
            <select value={clusterCode} onChange={(e) => setClusterCode(e.target.value)}>
              <option value="">Select...</option>
              {clusters.map((c) => <option key={c.clusterId} value={c.clusterCode}>{c.clusterCode}</option>)}
            </select>
          </div>
          <div className="form-row">
            <label>Horizon (days)</label>
            <select value={horizon} onChange={(e) => setHorizon(+e.target.value)}>
              <option value={30}>30</option><option value={60}>60</option><option value={90}>90</option><option value={180}>180</option>
            </select>
          </div>
        </div>
        <button disabled={!clusterCode || loading} onClick={run}>{loading ? "Forecasting..." : "Run forecast"}</button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {forecast && (
        <>
          <ForecastRow label="CPU" forecast={forecast.cpu} />
          <ForecastRow label="Memory" forecast={forecast.memory} />
          <ForecastRow label="Storage" forecast={forecast.storage} />
        </>
      )}
    </>
  );
}
