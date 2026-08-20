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
  decided,
  onDecide,
}: {
  payload: NonNullable<RunInvestigationResult["review_payload"]>;
  investigationId: number;
  busy: boolean;
  /** True once this investigation has a decision. The parent normally swaps
   *  this panel for a summary, but a resume that returns AwaitingReview again
   *  re-renders it - and live buttons on a decided investigation are worse
   *  than a disabled panel. */
  decided?: boolean;
  onDecide: (id: number, decision: "Approve" | "Reject", cluster?: string, host?: string) => void;
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

  function choose(option: ReviewOption, hostName?: string) {
    setCluster(option.cluster_code);
    setHost(hostName ?? option.hosts[0]?.host_name);
  }

  // Older payloads (before options existed) have no capacity to show - fall
  // back to the flat list rather than rendering an empty panel.
  if (options.length === 0) {
    return (
      <div>
        <p>{payload.message}</p>
        <p>{payload.top_candidates.join(", ")}</p>
        <div className="chat-review-actions">
          <button disabled={busy || decided} onClick={() => onDecide(investigationId, "Approve")}>
            Approve
          </button>
          <button className="danger" disabled={busy || decided} onClick={() => onDecide(investigationId, "Reject")}>
            Reject
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <p>{payload.message}</p>

      {options.map((option) => {
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
                disabled={isRejected || busy}
                onChange={() => choose(option)}
              />
              <strong>{option.cluster_code}</strong>
              <span className={`badge ${isRejected ? "rejected" : "eligible"}`}>
                {option.eligibility_status}
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
                            disabled={isRejected || busy}
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
        {/* Disabled while a request is in flight: these controls stay rendered
            after a decision (the thread keeps its history), so without this a
            second click re-resumes an already-decided investigation. */}
        <button
          disabled={busy || decided || !cluster}
          onClick={() => onDecide(investigationId, "Approve", cluster, host)}
        >
          {cluster ? `Approve ${cluster}${host ? ` / ${host}` : ""}` : "Approve"}
        </button>
        <button className="danger" disabled={busy || decided} onClick={() => onDecide(investigationId, "Reject")}>
          Reject all
        </button>
      </div>
    </div>
  );
}
