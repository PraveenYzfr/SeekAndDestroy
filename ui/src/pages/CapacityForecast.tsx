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
  // Narration is a second, opt-in request. The numbers arrive first and are
  // never delayed by a model call - and a reader who only wants the figures
  // never pays for prose.
  const [explaining, setExplaining] = useState(false);

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

  async function explain() {
    if (!clusterCode) return;
    setExplaining(true);
    setError(null);
    try {
      setForecast(await api.getForecast(clusterCode, horizon, true));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setExplaining(false);
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

          <div className="card">
            {forecast.explanation ? (
              <>
                <strong>
                  What this means
                  {forecast.explained_resource && (
                    <span className="stat-label" style={{ marginLeft: 8 }}>
                      {forecast.explained_resource} is the binding resource
                    </span>
                  )}
                </strong>
                <p style={{ marginTop: 8 }}>{forecast.explanation.summary}</p>
                {forecast.explanation.recommended_action && (
                  <div className="explain-box">{forecast.explanation.recommended_action}</div>
                )}
              </>
            ) : (
              <>
                <button className="secondary" disabled={explaining} onClick={explain}>
                  {explaining ? "Asking..." : "Explain this forecast"}
                </button>
                <p className="subtitle" style={{ marginTop: 8, marginBottom: 0 }}>
                  {/* Said plainly, because the numbers above are the product and
                      the prose is not: it can fail, and when it does the figures
                      are unaffected. */}
                  Adds a written summary of the resource that runs out first. The figures above are
                  computed and never change.
                </p>
              </>
            )}
          </div>
        </>
      )}
    </>
  );
}
