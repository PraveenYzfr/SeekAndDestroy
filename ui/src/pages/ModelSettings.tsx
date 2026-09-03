import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { EvaluationModel, EvaluationResult, ModelProvider, ModelRole } from "../types";

/** One provider+model choice, wired to assign/reset. Used twice per role card -
 *  once for the primary model, once for its fallback - so the two behave
 *  identically instead of drifting into two slightly different controls.
 *
 *  `roleKey` is what gets sent to the API: the role's own name for the
 *  primary, "<role>.fallback" for the backup - see ModelRoleFallback.role.
 *  Passing an empty `provider`/`model` (the unconfigured-fallback case) is
 *  valid: PENDING then has nothing to prefill, so both selects start on
 *  "choose one" rather than showing a pairing that does not exist. */
function RoleModelPicker({
  roleKey,
  provider,
  model,
  providers,
  pending,
  setPending,
  busy,
  assign,
}: {
  roleKey: string;
  provider: string;
  model: string;
  providers: ModelProvider[];
  pending: Record<string, { provider: string; model: string }>;
  setPending: React.Dispatch<React.SetStateAction<Record<string, { provider: string; model: string }>>>;
  busy: string | null;
  assign: (role: string, value: string) => void;
}) {
  const pendingHere = pending[roleKey];
  return (
    <div style={{ display: "flex", gap: 8 }}>
      <select
        aria-label="Provider"
        disabled={busy === roleKey}
        value={pendingHere?.provider ?? provider}
        onChange={(e) =>
          setPending((prev) => ({
            ...prev,
            // Model deliberately cleared: the previous model belongs to the
            // previous provider, and carrying it across would offer a
            // pairing that does not exist.
            [roleKey]: { provider: e.target.value, model: "" },
          }))
        }
        style={{ flex: "0 0 40%" }}
      >
        {/* No provider chosen at all yet (an unconfigured fallback) - an empty
            option so the select does not silently default to the first
            provider in the list without the operator having picked it. */}
        {!provider && !pendingHere && <option value="">not set</option>}
        {/* The configured provider may not be reachable right now - a key
            removed, an outage. It stays selectable so the screen shows what
            IS configured rather than quietly implying something else. */}
        {provider && !providers.some((p) => p.provider === provider && p.available) && (
          <option value={provider}>{provider} (unavailable)</option>
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
        disabled={busy === roleKey}
        value={pendingHere?.model ?? model}
        onChange={(e) => {
          const chosenProvider = pendingHere?.provider ?? provider;
          if (e.target.value) void assign(roleKey, `${chosenProvider}::${e.target.value}`);
        }}
        style={{ flex: 1 }}
      >
        {/* Prompt shown only while a provider change is pending, so the select
            never sits on a blank that looks like a cleared setting. */}
        {pendingHere && !pendingHere.model && <option value="">select a model...</option>}
        {!pendingHere && !model && <option value="">not set</option>}
        {/* The model in effect may have been retired since it was chosen.
            Showing it keeps the control honest about what is actually
            running rather than displaying a neighbour. */}
        {!pendingHere && model && !providers.some((p) => p.provider === provider && p.models.includes(model)) && (
          <option value={model}>{model} (not in the current list)</option>
        )}
        {(providers.find((p) => p.provider === (pendingHere?.provider ?? provider))?.models ?? []).map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
    </div>
  );
}

/** WHICH LAYER decided this role's model, in words an operator can act on.
 *
 *  Everything that was not an override used to read "from config", which is
 *  where this screen sends someone to change it - and for a TIER resolution
 *  that is the wrong file section: editing SAD_LLM__MODEL does nothing to a
 *  role answering from the cheap slot. Three of the five layers were invisible.
 */
function sourceLabel(role: ModelRole): string {
  switch (role.source) {
    case "override":
      return "overridden";
    case "force_single":
      return "forced single model";
    case "judge_default":
      return "judge default";
    case "tier":
      return role.tier ? `from config, ${role.tier} tier` : "from config, tier";
    default:
      return "from config";
  }
}

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

  /** `quiet` refreshes WITHOUT blanking the page.
   *
   *  Every save used to call load(), which set loading=true and replaced the
   *  whole screen with "Loading model settings..." - so choosing a model threw
   *  away every role card, every open dropdown and the scroll position, then
   *  rebuilt them. That is the jitter. It also re-asked every provider for its
   *  model list, which is the slowest thing this screen does, to redraw values
   *  the save response already carried. */
  async function load(refreshProviders = false, quiet = false) {
    if (!quiet) setLoading(true);
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

  /** Fold one saved assignment into the roles already on screen.
   *
   *  `roleKey` is either a role's own name or "<role>.fallback", so this looks
   *  in both places - the same key the API takes, rather than a second mapping
   *  that could disagree with it.
   *
   *  Every field comes from the SERVER'S response. Nothing is assembled here:
   *  a "set by" or a timestamp invented in the browser would look identical to
   *  a stored one and outlive anyone's memory of where it came from. */
  function applySaved(
    roleKey: string,
    saved: { provider: string; model: string; source: ModelRole["source"]; updated_by: string | null; updated_at: string | null },
  ) {
    setRoles((prev) =>
      prev.map((r) => {
        if (r.name === roleKey) {
          return {
            ...r,
            provider: saved.provider,
            model: saved.model,
            source: saved.source,
            updated_by: saved.updated_by,
            updated_at: saved.updated_at,
          };
        }
        if (r.fallback.role === roleKey) {
          return {
            ...r,
            fallback: { ...r.fallback, provider: saved.provider, model: saved.model, configured: true },
          };
        }
        return r;
      }),
    );
  }

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
          // "Applies to the next investigation" was appended here and is ALREADY
          // stated permanently in the subtitle above. Repeating it per save
          // bought nothing and pushed the line into a second row.
          : `${role}: now ${provider} / ${model}.`,
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
      // The save response already describes the stored row, so the screen
      // updates in place. No refetch, no repaint, nothing to scroll back to.
      applySaved(role, result);
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
      setStatus(
        result.removed
          ? `${role}: reset to ${result.provider} / ${result.model}.`
          : `${role}: was already the default.`,
      );
      setPending((prev) => {
        const next = { ...prev };
        delete next[role];
        return next;
      });
      // Same in-place update as a save. The response carries what the role
      // resolves to now, INCLUDING which layer answered - so a reset that lands
      // on the tier slot does not get labelled "from config".
      setRoles((prev) =>
        prev.map((r) => {
          if (r.name === role) {
            return {
              ...r,
              provider: result.provider,
              model: result.model,
              source: result.source,
              updated_by: null,
              updated_at: null,
            };
          }
          if (r.fallback.role === role) {
            return { ...r, fallback: { ...r.fallback, provider: null, model: null, configured: false } };
          }
          return r;
        }),
      );
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

      {/* ALWAYS RENDERED, even with nothing to say.
          Conditional rendering made this the third layout shift on every save:
          a card appeared where none had been and pushed every role card down by
          its height, so the row being edited moved out from under the pointer.
          Reserving the space costs one blank line and removes the jump.

          aria-live because the save is now SILENT - nothing flashes, nothing
          reloads - and a confirmation nobody can perceive is not one. */}
      <div
        aria-live="polite"
        style={{
          // TWO LINES, because one was not enough. Reserving a single line still
          // shifted the page by 12px (measured): the confirmation wraps at this
          // width, so the band grew when it filled. Reserving the height the
          // message actually takes is the whole point of reserving it.
          minHeight: 38,
          fontSize: 13,
          margin: "8px 0",
          opacity: status ? 1 : 0,
          transition: "opacity 120ms ease-in",
        }}
      >
        {status}
      </div>

      {roles.map((role) => (
        <div className="card" key={role.name}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <strong>{role.title}</strong>
            <span className={`badge ${role.source === "override" ? "eligible" : ""}`}>
              {sourceLabel(role)}
            </span>
          </div>
          <p style={{ fontSize: 13, margin: "6px 0" }}>{role.description}</p>

          <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 8 }}>
            Routes: {role.chains.join(", ")}
          </div>

          <div className="form-row" style={{ maxWidth: 520 }}>
            <label>
              In effect: {role.provider} / {role.model}
            </label>
            {/* ITS OWN LINE, ALWAYS PRESENT.
                Appended to the label above, "- set by E1001" wrapped it onto a
                second line the moment a role became overridden, moving
                everything below by one line height (16px, measured). A reserved
                line costs that space once instead of paying it on every save. */}
            <div style={{ minHeight: 16, fontSize: 12, opacity: 0.7 }}>
              {role.source === "override" && role.updated_by ? `set by ${role.updated_by}` : ""}
            </div>
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
            <RoleModelPicker
              roleKey={role.name}
              provider={role.provider}
              model={role.model}
              providers={providers}
              pending={pending}
              setPending={setPending}
              busy={busy}
              assign={assign}
            />
          </div>

          {/* ALWAYS RENDERED, DISABLED WHEN IT DOES NOT APPLY.
              Rendering this conditionally was the largest of the layout shifts:
              saving a model made the role "overridden", which made this button
              appear, which pushed every card below it down by 67px - measured.
              So the reward for choosing a model was the rest of the page
              jumping out from under the pointer.

              Disabled rather than hidden because the affordance is worth
              knowing about before it is usable: "this can be undone" is the
              reassurance somebody wants BEFORE they change a model, not after. */}
          <button
            className="secondary"
            disabled={busy === role.name || role.source !== "override"}
            onClick={() => void reset(role.name)}
            style={{ marginTop: 8 }}
            title={
              role.source === "override"
                ? "Drop the override so this role follows configuration again"
                : "Nothing to reset - this role already follows configuration"
            }
          >
            Reset to configured default
          </button>

          {/* WHAT ANSWERS WHEN THE PRIMARY FAILS - never chosen automatically.
              Per role rather than one estate-wide spare: extraction wants
              strict schema adherence, reporting wants readable prose, and a
              single substitute is right for at most one of them. Being wrong
              for the others is a quiet drop in output quality, not an error -
              exactly the failure nobody investigates. Unconfigured is a
              legitimate, common state, not an incomplete one: the role simply
              answers alone if its primary fails, same as before fallbacks
              existed. */}
          <div className="form-row" style={{ maxWidth: 520, marginTop: 14 }}>
            <label>
              Fallback, if {role.provider} / {role.model} fails:{" "}
              {role.fallback.configured ? (
                `${role.fallback.provider} / ${role.fallback.model}`
              ) : role.fallback.chain.length > 0 ? (
                /* NOT "not set". An unconfigured role inherits the estate chain,
                   and this label denied it - so the screen under-reported the
                   platform's resilience, which invites pinning a fallback that
                   was already there. Shown greyed to keep the distinction that
                   matters: inherited, not chosen here. */
                <span style={{ opacity: 0.75 }}>
                  {role.fallback.chain.map((leg, i) => {
                    const reachable = providers.some((p) => p.provider === leg.provider && p.available);
                    return (
                      <span key={leg.provider}>
                        {i > 0 ? ", then " : ""}
                        <span
                          style={reachable ? undefined : { textDecoration: "line-through" }}
                          title={reachable ? undefined : `${leg.provider} cannot be reached, so this leg is skipped`}
                        >
                          {leg.provider} / {leg.model}
                        </span>
                      </span>
                    );
                  })}
                  {" (inherited)"}
                </span>
              ) : (
                "none - this role answers alone if its provider fails"
              )}
            </label>
            <RoleModelPicker
              roleKey={role.fallback.role}
              provider={role.fallback.provider ?? ""}
              model={role.fallback.model ?? ""}
              providers={providers}
              pending={pending}
              setPending={setPending}
              busy={busy}
              assign={assign}
            />
          </div>

          {/* Same reasoning as the reset above: appearing and disappearing is
              what moved the page. */}
          <button
            className="secondary"
            disabled={busy === role.fallback.role || !role.fallback.configured}
            onClick={() => void reset(role.fallback.role)}
            style={{ marginTop: 8 }}
            title={
              role.fallback.configured
                ? "Drop this role's explicit fallback and inherit the configured chain"
                : "No explicit fallback to clear - this role already inherits the configured chain"
            }
          >
            Clear fallback
          </button>
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
