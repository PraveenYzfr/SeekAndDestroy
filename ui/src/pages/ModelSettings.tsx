import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { EvaluationModel, EvaluationResult, ModelProvider, ModelRole } from "../types";

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
  //: A provider chosen but not yet paired with a model. Held per role
  //: rather than globally: an administrator narrowing one role must not
  //: change what another role is offering.
  const [pending, setPending] = useState<Record<string, { provider: string; model: string }>>({});
  const [evaluation, setEvaluation] = useState<EvaluationResult | null>(null);
  const [grading, setGrading] = useState(false);

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
      // Drop the pending pair only on success. Leaving it on failure keeps the
      // administrator's half-made choice on screen instead of snapping the row
      // back to what is still in effect, which would read as though the change
      // had been applied and then undone.
      setPending((prev) => {
        const next = { ...prev };
        delete next[role];
        return next;
      });
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
      setPending((prev) => {
        const next = { ...prev };
        delete next[role];
        return next;
      });
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
            {/* TWO SELECTS, NOT ONE GROUPED LIST.
                A single select with an optgroup per provider meant scrolling
                one list of every model on every provider - openai alone
                enumerates 124. Choosing a provider first turns "find your model
                among 190" into "pick one of five, then one of a dozen".

                The provider select is deliberately NOT the same control as the
                model select: switching provider does not assign anything. It
                narrows the second list and waits, because a change here spends
                money on the next investigation and an accidental keystroke
                should not be able to do that. Nothing is written until a model
                is chosen. */}
            <div style={{ display: "flex", gap: 8 }}>
              <select
                aria-label="Provider"
                disabled={busy === role.name}
                value={pending[role.name]?.provider ?? role.provider}
                onChange={(e) =>
                  setPending((prev) => ({
                    ...prev,
                    // Model deliberately cleared: the previous model belongs to
                    // the previous provider, and carrying it across would offer
                    // a pairing that does not exist.
                    [role.name]: { provider: e.target.value, model: "" },
                  }))
                }
                style={{ flex: "0 0 40%" }}
              >
                {/* The configured provider may not be reachable right now - a
                    key removed, an outage. It stays selectable so the screen
                    shows what IS configured rather than quietly implying
                    something else. */}
                {!providers.some((p) => p.provider === role.provider && p.available) && (
                  <option value={role.provider}>{role.provider} (unavailable)</option>
                )}
                {providers
                  .filter((p) => p.available)
                  .map((p) => (
                    <option key={p.provider} value={p.provider}>
                      {p.provider}
                    </option>
                  ))}
              </select>

              <select
                aria-label="Model"
                disabled={busy === role.name}
                value={pending[role.name]?.model ?? role.model}
                onChange={(e) => {
                  const provider = pending[role.name]?.provider ?? role.provider;
                  if (e.target.value) void assign(role.name, `${provider}::${e.target.value}`);
                }}
                style={{ flex: 1 }}
              >
                {/* Prompt shown only while a provider change is pending, so the
                    select never sits on a blank that looks like a cleared
                    setting. */}
                {pending[role.name] && !pending[role.name].model && (
                  <option value="">select a model...</option>
                )}
                {/* The model in effect may have been retired since it was
                    chosen. Showing it keeps the control honest about what is
                    actually running rather than displaying a neighbour. */}
                {!pending[role.name] &&
                  !providers.some(
                    (p) => p.provider === role.provider && p.models.includes(role.model),
                  ) && <option value={role.model}>{role.model} (not in the current list)</option>}
                {(
                  providers.find(
                    (p) => p.provider === (pending[role.name]?.provider ?? role.provider),
                  )?.models ?? []
                ).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
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
        {/* The note explains WHICH evaluation - the deterministic graders use no
            model, the judge role above does. It used to say evaluation had no
            model at all, which contradicted the judge row on the same screen. */}
        <p style={{ fontSize: 13, marginTop: 6, whiteSpace: "pre-line" }}>{note}</p>

        {/* RUNNABLE FROM HERE, because the alternative was not runnable at all.
            This card used to say "run scripts/evaluate.py" - and scripts/ is not
            in the service image, so on the deployed system nobody could follow
            that instruction. An acceptance gate nobody can run is not a gate,
            and per-role model switching is exactly the change that needs one. */}
        <button
          className="secondary"
          disabled={grading}
          onClick={() => {
            setGrading(true);
            setStatus("");
            api
              .getEvaluation()
              .then(setEvaluation)
              .catch((e) => setStatus(e instanceof Error ? e.message : String(e)))
              .finally(() => setGrading(false));
          }}
          style={{ marginTop: 8 }}
        >
          {grading ? "Grading..." : "Run evaluation"}
        </button>
        <span style={{ fontSize: 12, opacity: 0.75, marginLeft: 10 }}>
          Grades calls already made. No model is called and nothing is spent.
        </span>

        {evaluation && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 6 }}>
              {evaluation.calls_seen} recorded calls graded
            </div>
            {evaluation.models.length === 0 && (
              <p style={{ fontSize: 13 }}>
                No graded calls yet. Run an investigation, then grade again.
              </p>
            )}
            {evaluation.models.map((m: EvaluationModel) => (
              <div key={m.model} style={{ marginBottom: 12 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{m.model}</div>
                <div style={{ fontSize: 12, opacity: 0.8, margin: "2px 0 6px" }}>
                  {m.generated} generated, {m.cached} cached (never graded), {m.failures} failed,{" "}
                  {m.ungradeable} ungradeable &middot; p50{" "}
                  {m.latency_p50_ms === null ? "-" : `${(m.latency_p50_ms / 1000).toFixed(1)}s`} &middot; p95{" "}
                  {m.latency_p95_ms === null ? "-" : `${(m.latency_p95_ms / 1000).toFixed(1)}s`}
                </div>
                <table style={{ fontSize: 12, width: "100%", maxWidth: 520 }}>
                  <tbody>
                    {Object.entries(m.properties).map(([name, v]) => (
                      <tr key={name}>
                        <td style={{ opacity: 0.85 }}>{name.replace(/_/g, " ")}</td>
                        {/* The denominator is part of the claim, not a footnote.
                            A rate over a handful of observations is not the same
                            statement as one over hundreds. */}
                        <td style={{ textAlign: "right" }}>
                          {v.rate === null ? "-" : `${(v.rate * 100).toFixed(1)}%`}
                        </td>
                        <td style={{ textAlign: "right", opacity: 0.7 }}>
                          over {v.observations}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
            {evaluation.flagged.length > 0 && (
              <details style={{ fontSize: 12, marginTop: 4 }}>
                <summary>{evaluation.flagged.length} flagged figures</summary>
                <ul style={{ margin: "6px 0 0 16px" }}>
                  {evaluation.flagged.slice(0, 12).map((f, i) => (
                    <li key={i}>
                      audit {f.audit_id} &middot; {f.schema} &middot; {f.property}:{" "}
                      {f.ungrounded.join(", ")}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </div>

      <button className="secondary" onClick={() => void load(true)}>
        Reload model lists from providers
      </button>
    </>
  );
}
