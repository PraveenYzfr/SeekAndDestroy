import { useState } from "react";
import type { CapacityView, ResourceCapacity, ReviewOption, RunInvestigationResult } from "@/types";

/**
 * Human review as a *choice*, not a rubber stamp.
 *
 * The reviewer is picking one placement for one workload, so approving a
 * shortlist of three records no decision at all - which is what a bare
 * Approve/Reject pair did. Here they select a cluster and a host, and that
 * selection is what gets approved; the rest are stored NotSelected.
 *
 * Each option shows total / used / free for CPU, memory and storage. A score
 * of 99.83 is a summary of those numbers, not a replacement for them - an
 * engineer approving a placement wants to see how much room is actually left
 * on the box.
 */

function fmt(value: number | null | undefined, unit = ""): string {
  if (value == null) return "—";
  const rounded = value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded.toLocaleString()}${unit}`;
}

function Bar({ percent }: { percent: number | null }) {
  const pct = Math.max(0, Math.min(100, percent ?? 0));
  // Colour tracks the same thresholds the capacity rules use (75/90), so what
  // the reviewer sees matches what the engine would allow.
  const colour = pct >= 90 ? "var(--red)" : pct >= 75 ? "var(--amber, #d68b21)" : "var(--green)";
  return (
    <span className="capacity-bar" title={`${fmt(percent, "%")} used`}>
      <span style={{ width: `${pct}%`, background: colour }} />
    </span>
  );
}

function ResourceRow({ label, unit, cap }: { label: string; unit: string; cap: ResourceCapacity }) {
  return (
    <tr>
      <td className="capacity-label">{label}</td>
      <td>{fmt(cap.total, unit)}</td>
      <td>{fmt(cap.used, unit)}</td>
      <td>
        <strong>{fmt(cap.free, unit)}</strong>
      </td>
      <td style={{ width: 110 }}>
        <Bar percent={cap.used_percent} /> {fmt(cap.used_percent, "%")}
      </td>
    </tr>
  );
}

function CapacityTable({ capacity }: { capacity: CapacityView | null }) {
  if (!capacity) return null;
  return (
    <table className="capacity-table">
      <thead>
        <tr>
          <th></th>
          <th>Total</th>
          <th>Used</th>
          <th>Free</th>
          <th>Utilisation</th>
        </tr>
      </thead>
      <tbody>
        <ResourceRow label="CPU" unit=" cores" cap={capacity.cpu_cores} />
        <ResourceRow label="Memory" unit=" GB" cap={capacity.memory_gb} />
        <ResourceRow label="Storage" unit=" GB" cap={capacity.storage_gb} />
      </tbody>
    </table>
  );
}

export default function ReviewChoice({
  payload,
  investigationId,
  busy,
  locked,
  lockedNote,
  onDecide,
}: {
  payload: NonNullable<RunInvestigationResult["review_payload"]>;
  investigationId: number;
  busy: boolean;
  /** True when this panel must not be acted on again: the investigation has a
   *  decision, or a later question has superseded it. The panel STAYS - a
   *  shortlist is the evidence behind what happened next, and removing it on
   *  decide left the thread with a one-line badge where the reasoning had
   *  been. Locked, not gone. */
  locked?: boolean;
  /** Why it is locked, shown in the panel. Silent locking reads as a bug. */
  lockedNote?: string;
  onDecide: (
    id: number,
    decision: "Approve" | "Reject" | "RequestMoreAnalysis",
    cluster?: string,
    host?: string,
  ) => void;
}) {
  const options: ReviewOption[] = payload.options ?? [];
  const eligible = options.filter((o) => o.eligibility_status === "Eligible");

  // Pre-select the top-ranked option: it is the platform's recommendation, and
  // making the reviewer re-pick it adds a click without adding a decision.
  const [cluster, setCluster] = useState<string | undefined>(eligible[0]?.cluster_code);
  const [host, setHost] = useState<string | undefined>(eligible[0]?.hosts[0]?.host_name);
  // Collapsed-set rather than a single expanded id: the figures are the
  // point of this panel, so every option shows them unless folded away.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [showRejected, setShowRejected] = useState(false);
  //  How far into the ranked deck the engineer has read. The server sends more
  //  options than it shows (policy.review_options against policy.top_clusters)
  //  precisely so this costs nothing: every candidate here was already scored
  //  by the run that produced the payload, so "show the next 3" is a slice,
  //  not a twenty-second re-walk of the estate.
  //
  //  Cumulative rather than a replacing page. The engineer paging for
  //  alternatives is comparing, and taking option 2 off the screen to show
  //  option 5 loses the thing they were comparing against.
  const pageSize = payload.page_size ?? 3;
  const [revealed, setRevealed] = useState(pageSize);
  const notRecommended = options.filter((o) => o.eligibility_status !== "Eligible");
  const shownEligible = eligible.slice(0, revealed);
  const moreEligible = Math.max(0, eligible.length - shownEligible.length);
  const visible = showRejected ? [...shownEligible, ...notRecommended] : shownEligible;
  const steps = payload.next_steps;
  const disabled = busy || Boolean(locked);

  function choose(option: ReviewOption, hostName?: string) {
    setCluster(option.cluster_code);
    setHost(hostName ?? option.hosts[0]?.host_name);
  }

  // Older payloads (before options existed) have no capacity to show - fall
  // back to the flat list rather than rendering an empty panel.
  if (options.length === 0) {
    return (
      <div style={locked ? { opacity: 0.6 } : undefined}>
      {lockedNote && (
        //  Says WHY it is inert. A greyed panel with no explanation reads as a
        //  bug, and the two reasons it greys - decided, or superseded by a
        //  later question - mean different things to the reader.
        <p className="stat-label" style={{ marginBottom: 8 }}>{lockedNote}</p>
      )}
        <p>{payload.message}</p>
        <p>{payload.top_candidates.join(", ")}</p>
        {notRecommended.length > 0 && (
        <button
          className="secondary"
          style={{ marginBottom: 10 }}
          onClick={() => setShowRejected(!showRejected)}
        >
          {showRejected
            ? `Hide ${notRecommended.length} not recommended`
            : `Show ${notRecommended.length} cluster(s) that were not recommended`}
        </button>
      )}

      <div className="chat-review-actions">
          <button disabled={disabled} onClick={() => onDecide(investigationId, "Approve")}>
            Select and proceed
          </button>
          <button className="secondary" disabled={disabled} onClick={() => onDecide(investigationId, "RequestMoreAnalysis")}>
            Next choices
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={locked ? { opacity: 0.6 } : undefined}>
      {lockedNote && (
        //  Says WHY it is inert. A greyed panel with no explanation reads as a
        //  bug, and the two reasons it greys - decided, or superseded by a
        //  later question - mean different things to the reader.
        <p className="stat-label" style={{ marginBottom: 8 }}>{lockedNote}</p>
      )}
      <p>{eligible.length === 0 && steps ? "Nothing here can take this workload." : payload.message}</p>

      {/* Only present when this run actually excluded a data center - see
          app/services/refinement.py::data_center_choice. On this estate a
          Tier-1 workload typically spans two DCs and three eligible
          clusters total, so after one exclusion "nothing else has room" is
          the common outcome, not a rare one - stated plainly rather than
          left to an empty options list to imply. */}
      {payload.data_center_choice && (
        <div className="explain-box" style={{ marginBottom: 10 }}>
          <strong>Data centre choice</strong>
          <p style={{ fontSize: 13, margin: "6px 0 0" }}>
            Excluding {payload.data_center_choice.excluded_data_centers.join(", ")}:{" "}
            {payload.data_center_choice.has_genuine_alternative ? (
              payload.data_center_choice.available_data_centers.length === 1 ? (
                <>
                  only <strong>{payload.data_center_choice.available_data_centers[0].data_center}</strong> still has
                  eligible capacity ({payload.data_center_choice.available_data_centers[0].eligible_count} cluster
                  {payload.data_center_choice.available_data_centers[0].eligible_count === 1 ? "" : "s"}).
                </>
              ) : (
                <>these data centres still have eligible capacity:</>
              )
            ) : (
              <>no data centre still has eligible capacity for this workload.</>
            )}
          </p>
          {payload.data_center_choice.has_genuine_alternative &&
            payload.data_center_choice.available_data_centers.length > 1 && (
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {payload.data_center_choice.available_data_centers.map((dc) => (
                  <li key={dc.data_center}>
                    {dc.data_center} — {dc.eligible_count} eligible cluster{dc.eligible_count === 1 ? "" : "s"}
                  </li>
                ))}
              </ul>
            )}
        </div>
      )}

      {/* The next move, when there is one worth offering. This is a search: with
          a usable shortlist `sufficient` is true and none of this renders - the
          reader picks one and leaves. When it is not, a choice beats an
          explanation of every rule that failed. Each option carries what it
          would actually buy, computed from the candidates already evaluated. */}
      {/* Nothing recommended AND nothing to offer. Without this the panel is
          simply absent, which looks identical to a good result - and at demo
          time "no suggestions because everything is fine" and "no suggestions
          because the field never arrived" are the two things you most need to
          tell apart. Says so rather than rendering nothing. */}
      {eligible.length === 0 && (!steps || steps.choices.length === 0) && (
        <div className="explain-box" style={{ marginBottom: 10 }}>
          <strong>No options and nothing to relax.</strong>
          <p style={{ fontSize: 13, margin: "6px 0 0" }}>
            Every cluster considered was blocked by something a smaller request would not
            change. Try a different environment, platform or location.
          </p>
        </div>
      )}

      {/* Moves, not a reading list. These were rendered as <li> text, which
          made "Show the next 3" a description of something the engineer could
          not do - the backend computed next_offset and the UI dropped it.
          show_more is answered here, from options already in the browser;
          the rest describe a different search and are left as text because
          this panel cannot run one. */}
      {steps && !steps.sufficient && steps.choices.length > 0 && (
        <div className="explain-box" style={{ marginBottom: 10 }}>
          <strong>What next?</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {steps.choices.map((choice, i) => (
              <li key={i}>
                {choice.action === "show_more" && moreEligible > 0 ? (
                  <button
                    className="secondary"
                    disabled={disabled}
                    onClick={() => setRevealed((n) => n + pageSize)}
                  >
                    Show {Math.min(pageSize, moreEligible)} more
                  </button>
                ) : (
                  choice.label
                )}
                {choice.detail ? <span className="stat-label"> — {choice.detail}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Not-recommended clusters are folded away by default. The chat used to
          render every one of them with a full capacity table and the line
          "cluster rejected - hosts are only ranked inside eligible clusters",
          so a search that found nothing produced three screens of tables for
          options the reader cannot choose. They stay reachable, because "why
          not that one" is fair, but they are not the answer. */}
      {visible.map((option) => {
        const isChosen = option.cluster_code === cluster;
        const isRejected = option.eligibility_status !== "Eligible";
        const isOpen = !collapsed.has(option.cluster_code);
        return (
          <div key={option.cluster_code} className={`review-option${isChosen ? " chosen" : ""}`}>
            <label className="review-option-head">
              <input
                type="radio"
                name={`option-${investigationId}`}
                checked={isChosen}
                disabled={isRejected || disabled}
                onChange={() => choose(option)}
              />
              <strong>{option.cluster_code}</strong>
              {/* The SITE, next to the name. Choosing between three clusters for
                  a Tier-1 workload is a decision about which data centre it
                  lands in, and this panel used to show only cluster codes -
                  atl-03 and den-03 are different sites and nothing said so.
                  It also makes "give me a different DC" legible: the reviewer
                  can see what they would be moving away from. */}
              {option.data_center && (
                <span className="badge" title="Data centre">
                  {option.data_center}
                </span>
              )}
              <span className={`badge ${isRejected ? "rejected" : "eligible"}`}>
                {isRejected ? "Not recommended" : "Recommended"}
              </span>
              <span className="stat-label">
                score {option.overall_score ?? "—"} · {fmt(option.projected_headroom_percent, "%")} headroom after
                placement
              </span>
              <button
                type="button"
                className="secondary"
                style={{ marginLeft: "auto" }}
                onClick={() =>
                  setCollapsed((prev) => {
                    const next = new Set(prev);
                    if (next.has(option.cluster_code)) next.delete(option.cluster_code);
                    else next.add(option.cluster_code);
                    return next;
                  })
                }
              >
                {isOpen ? "Hide" : "Capacity"}
              </button>
            </label>

            {isOpen && (
              <div className="review-option-body">
                <div className="stat-label">Cluster capacity</div>
                <CapacityTable capacity={option.capacity} />

                {option.hosts.length > 0 ? (
                  <>
                    <div className="stat-label" style={{ marginTop: 10 }}>
                      Choose a host
                    </div>
                    {option.hosts.map((h) => (
                      <div key={h.host_name} className="review-host">
                        <label>
                          <input
                            type="radio"
                            name={`host-${investigationId}-${option.cluster_code}`}
                            checked={isChosen && host === h.host_name}
                            disabled={isRejected || disabled}
                            onChange={() => choose(option, h.host_name)}
                          />
                          <strong>{h.host_name}</strong>
                          <span className="stat-label">
                            score {h.overall_score ?? "—"} · {fmt(h.projected_headroom_percent, "%")} headroom
                          </span>
                        </label>
                        <CapacityTable capacity={h.capacity} />
                      </div>
                    ))}
                  </>
                ) : (
                  <p className="stat-label">
                    {isRejected
                      ? "Cluster rejected - hosts are only ranked inside eligible clusters."
                      : "No eligible hosts in this cluster."}
                  </p>
                )}
              </div>
            )}
          </div>
        );
      })}

      <div className="chat-review-actions">
        {/* Disabled while a request is in flight, and while locked: these
            controls stay rendered after a decision because the shortlist is
            the evidence behind what happened next, so without this a second
            click re-resumes an already-decided investigation.

            SELECT AND PROCEED / NEXT CHOICES, not Approve / Reject. An
            engineer reading a shortlist is choosing where to put a workload,
            not sitting in judgement on the platform's homework, and "Reject
            all" made declining a page of options sound like a verdict on
            them. The recorded decision is unchanged - proceeding still writes
            Approve, because proceeding IS the placement decision that gets
            audited - but moving on now sends RequestMoreAnalysis, which the
            schema has always allowed and nothing ever sent. */}
        <button
          disabled={disabled || !cluster}
          onClick={() => onDecide(investigationId, "Approve", cluster, host)}
        >
          {cluster ? `Select ${cluster}${host ? ` / ${host}` : ""} and proceed` : "Select and proceed"}
        </button>
        <button
          className="secondary"
          disabled={disabled}
          onClick={() => onDecide(investigationId, "RequestMoreAnalysis", cluster, host)}
        >
          Next choices
        </button>
      </div>
    </div>
  );
}
