import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { CmdbApplication, CandidateScore } from "@/types";
import CandidateTable from "@/components/CandidateTable";

export default function HostingRecommendation() {
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [applications, setApplications] = useState<CmdbApplication[]>([]);
  const [selectedApp, setSelectedApp] = useState("");
  const [candidates, setCandidates] = useState<CandidateScore[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [form, setForm] = useState({
    environment: "Production", cpuCores: 8, memoryGb: 32, storageGb: 500, platform: "Kubernetes",
    availabilityTier: "Tier-2", dataClassification: "Internal", preferredLocation: "",
    expectedGrowthPercent: 10, requestedByEmployeeId: 1,
  });

  useEffect(() => {
    api.getApplications().then(setApplications).catch(() => undefined);
  }, []);

  async function submitExisting() {
    if (!selectedApp) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getHostingRecommendations(selectedApp);
      setCandidates(result.candidates);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function submitNew() {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getCapacityRecommendations(form);
      setCandidates(result.candidates);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <h2>Hosting Recommendation</h2>
      <p className="subtitle">Find the best infrastructure candidates for an application or a new workload.</p>

      <div className="card">
        <div style={{ marginBottom: 12 }}>
          <button className={mode === "existing" ? "" : "secondary"} onClick={() => setMode("existing")}>
            Existing application
          </button>{" "}
          <button className={mode === "new" ? "" : "secondary"} onClick={() => setMode("new")}>
            New workload requirement
          </button>
        </div>

        {mode === "existing" ? (
          <div className="form-row">
            <label>Application</label>
            <select value={selectedApp} onChange={(e) => setSelectedApp(e.target.value)}>
              <option value="">Select an application...</option>
              {applications.map((a) => (
                <option key={a.applicationId} value={a.applicationCode}>
                  {a.applicationCode} - {a.applicationName}
                </option>
              ))}
            </select>
            <button style={{ marginTop: 8, width: 160 }} disabled={!selectedApp || loading} onClick={submitExisting}>
              {loading ? "Searching..." : "Find candidates"}
            </button>
          </div>
        ) : (
          <div className="grid">
            <div className="form-row">
              <label>Environment</label>
              <select value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
                <option>Production</option><option>Staging</option><option>Test</option><option>Development</option>
              </select>
            </div>
            <div className="form-row">
              <label>CPU cores</label>
              <input type="number" value={form.cpuCores} onChange={(e) => setForm({ ...form, cpuCores: +e.target.value })} />
            </div>
            <div className="form-row">
              <label>Memory (GB)</label>
              <input type="number" value={form.memoryGb} onChange={(e) => setForm({ ...form, memoryGb: +e.target.value })} />
            </div>
            <div className="form-row">
              <label>Storage (GB)</label>
              <input type="number" value={form.storageGb} onChange={(e) => setForm({ ...form, storageGb: +e.target.value })} />
            </div>
            <div className="form-row">
              <label>Platform</label>
              <select value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value })}>
                <option>Kubernetes</option><option>VMware</option><option>OpenShift</option><option>BareMetal</option><option>Hyper-V</option>
              </select>
            </div>
            <div className="form-row">
              <label>Availability tier</label>
              <select value={form.availabilityTier} onChange={(e) => setForm({ ...form, availabilityTier: e.target.value })}>
                <option>Tier-1</option><option>Tier-2</option><option>Tier-3</option>
              </select>
            </div>
            <div className="form-row">
              <label>Data classification</label>
              <select value={form.dataClassification} onChange={(e) => setForm({ ...form, dataClassification: e.target.value })}>
                <option>Public</option><option>Internal</option><option>Confidential</option><option>Restricted</option>
              </select>
            </div>
            <div className="form-row">
              <label>Preferred location (optional)</label>
              <input value={form.preferredLocation} onChange={(e) => setForm({ ...form, preferredLocation: e.target.value })} />
            </div>
            <div className="form-row">
              <label>Expected annual growth (%)</label>
              <input type="number" value={form.expectedGrowthPercent} onChange={(e) => setForm({ ...form, expectedGrowthPercent: +e.target.value })} />
            </div>
            <button style={{ alignSelf: "end" }} disabled={loading} onClick={submitNew}>
              {loading ? "Searching..." : "Find candidates"}
            </button>
          </div>
        )}
      </div>

      {error && <div className="error-box">{error}</div>}

      {candidates && (
        <div className="card">
          <strong>Ranked candidates ({candidates.filter((c) => c.eligibility_status === "Eligible").length} eligible of {candidates.length})</strong>
          <div style={{ marginTop: 10 }}>
            <CandidateTable candidates={candidates} />
          </div>
        </div>
      )}
    </>
  );
}
