import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ModelProvider, ModelRole } from "../types";

/** Administrator screen: which model runs each role.
 *
 *  Three things this screen deliberately does:
 *
 *  - Names the chain functions each role routes. "Narration" means nothing on
 *    its own; "explain_candidate, explain_forecast" is checkable.
 *  - Shows where each model came from - config or override - because that is
 *    what decides whether editing settings.py would affect the role at all.
 *  - Lists models fetched live from each provider. A hardcoded list has broken
 *    this estate five times, and a stale dropdown fails at run time in a place
 *    with no visible connection to this page.
 */
export default function ModelSettings() {
  const [roles, setRoles] = useState<ModelRole[]>([]);
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState("");

  async function load(refreshProviders = false) {
    setLoading(true);
    setError("");
    try {
      const [r, p] = await Promise.all([api.getModelRoles(), api.getModelProviders(refreshProviders)]);
      setRoles(r.roles);
      setNote(r.evaluation_note);
      setProviders(p.providers);
    } catch (e) {
      // A non-administrator gets 403 here. Say so plainly rather than showing
      // an empty page that looks broken.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function assign(role: string, value: string) {
    if (!value) return;
    const [provider, ...rest] = value.split("::");
    const model = rest.join("::");
    setBusy(role);
    setStatus("");
    try {
      const result = await api.setModelRole(role, provider, model);
      setStatus(
        result.unverified
          ? `${role}: saved as ${provider} / ${model}, but ${provider} could not be reached to confirm the model exists.`
          : `${role}: now ${provider} / ${model}. Applies to the next investigation.`,
      );
      await load();
    } catch (e) {
      setStatus(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function reset(role: string) {
    setBusy(role);
    setStatus("");
    try {
      const result = await api.clearModelRole(role);
      setStatus(result.removed ? `${role}: reset to the configured default.` : `${role}: was already the default.`);
      await load();
    } catch (e) {
      setStatus(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (loading) return <p>Loading model settings...</p>;
  if (error) return <div className="error-box">{error}</div>;

  const unavailable = providers.filter((p) => !p.available);

  return (
    <>
      <h2>Model Settings</h2>
      <p className="subtitle">
        Which model runs each part of an investigation. Changes apply to the next investigation, never to one
        already running.
      </p>

      {unavailable.length > 0 && (
        <div className="card">
          <strong>Providers that could not be reached</strong>
          <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            {unavailable.map((p) => (
              <li key={p.provider} className="rule-fail">
                {p.provider}: {p.error}
              </li>
            ))}
          </ul>
          <p style={{ fontSize: 13, marginTop: 8 }}>
            Their models are not listed below. Nothing is assumed on their behalf - a remembered list is how a
            retired model gets chosen.
          </p>
        </div>
      )}

      {status && (
        <div className="card" style={{ fontSize: 13 }}>
          {status}
        </div>
      )}

      {roles.map((role) => (
        <div className="card" key={role.name}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>{role.title}</strong>
            <span className={`badge ${role.source === "override" ? "eligible" : ""}`}>
              {role.source === "override" ? "overridden" : "from config"}
            </span>
          </div>
          <p style={{ fontSize: 13, margin: "6px 0" }}>{role.description}</p>

          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 8 }}>
            Routes: {role.chains.join(", ")}
          </div>

          <div className="form-row" style={{ maxWidth: 520 }}>
            <label>
              In effect: {role.provider} / {role.model}
              {role.source === "override" && role.updated_by ? ` — set by ${role.updated_by}` : ""}
            </label>
            <select
              disabled={busy === role.name}
              value={`${role.provider}::${role.model}`}
              onChange={(e) => void assign(role.name, e.target.value)}
            >
              {/* The current value may not appear in any provider listing - it
                  could have been retired since it was chosen. Showing it keeps
                  the select honest about what is actually configured. */}
              {!providers.some((p) => p.provider === role.provider && p.models.includes(role.model)) && (
                <option value={`${role.provider}::${role.model}`}>
                  {role.provider} / {role.model} (not in the current list)
                </option>
              )}
              {providers
                .filter((p) => p.available)
                .map((p) => (
                  <optgroup key={p.provider} label={p.provider}>
                    {p.models.map((m) => (
                      <option key={`${p.provider}::${m}`} value={`${p.provider}::${m}`}>
                        {p.provider} / {m}
                      </option>
                    ))}
                  </optgroup>
                ))}
            </select>
          </div>

          {role.source === "override" && (
            <button
              className="secondary"
              disabled={busy === role.name}
              onClick={() => void reset(role.name)}
              style={{ marginTop: 8 }}
            >
              Reset to configured default
            </button>
          )}
        </div>
      ))}

      <div className="card">
        <strong>Evaluation</strong>
        <p style={{ fontSize: 13, marginTop: 6 }}>{note}</p>
        <p style={{ fontSize: 13 }}>
          So the way to compare two models is to assign one here, run some investigations, then run{" "}
          <code>scripts/evaluate.py</code>. It grades calls that already happened, from the audit log, so it costs
          a table scan rather than a provider bill.
        </p>
      </div>

      <button className="secondary" onClick={() => void load(true)}>
        Reload model lists from providers
      </button>
    </>
  );
}
