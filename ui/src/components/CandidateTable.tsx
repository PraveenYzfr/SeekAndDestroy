import { Fragment, useState } from "react";
import type { CandidateScore } from "@/types";

/** The shortlist, as a recommendation rather than an engine dump.
 *
 *  Three things this deliberately does NOT do any more:
 *
 *  1. It does not show rejected clusters in the recommendations. The table used
 *     to run 1, 2, 3, then jump to 26, 27, 28 - listing the things it had just
 *     said not to use, in the section headed "recommendations". They are still
 *     available, one click away, because "why not that one" is a fair question;
 *     they are no longer the default answer to "where should this go".
 *
 *  2. It does not say "Eligible" and "Rejected". Those are the eligibility
 *     engine's words for its own output. A person reading a recommendation
 *     wants to know what is recommended.
 *
 *  3. It does not list ten PASS lines in the detail panel. On a recommended
 *     cluster every rule passed - that is what recommended means - so the
 *     interesting content is the score breakdown. Only failures are worth
 *     enumerating, and only the ones that are not recommended have any.
 */

function firstFailure(candidate: CandidateScore): string | null {
  const failed = (candidate.rule_results ?? []).find((r) => !r.passed);
  return failed ? failed.reason : null;
}

export default function CandidateTable({ candidates }: { candidates: CandidateScore[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const [showRejected, setShowRejected] = useState(false);

  const recommended = candidates.filter((c) => c.eligibility_status === "Eligible");
  const notRecommended = candidates.filter((c) => c.eligibility_status !== "Eligible");

  return (
    <>
      {recommended.length === 0 && (
        <div className="error-box">
          Nothing here can take this workload. {notRecommended.length} cluster(s) were considered - open the
          list below to see what blocked them.
        </div>
      )}

      {recommended.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Rank</th>
              <th>Cluster</th>
              <th>Score</th>
              {/* Free capacity, not cost: an engineer choosing where to place a
                  workload is deciding on available room, and the internal
                  chargeback rate was noise in that decision. */}
              <th>Free CPU</th>
              <th>Free RAM</th>
              <th>Headroom %</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {recommended.map((c) => (
              <Fragment key={c.cluster_id}>
                <tr>
                  <td>{c.rank}</td>
                  <td>
                    <strong>{c.cluster_code}</strong>
                  </td>
                  <td>{c.overall_score ?? "—"}</td>
                  <td>{c.snapshot ? `${c.snapshot.available_cpu_cores} cores` : "—"}</td>
                  <td>{c.snapshot ? `${c.snapshot.available_memory_gb} GB` : "—"}</td>
                  <td>{c.projected ? `${c.projected.projected_headroom_percent}%` : "—"}</td>
                  <td>
                    <button
                      className="secondary"
                      onClick={() => setExpanded(expanded === c.cluster_id ? null : c.cluster_id)}
                    >
                      {expanded === c.cluster_id ? "Hide" : "Detail"}
                    </button>
                  </td>
                </tr>

                {/* Hosts inside this cluster. Their scores are only comparable
                    to their siblings, never to the cluster row above - the node
                    capacity sub-score uses a different scale (see
                    docs/scoring-model.md). */}
                {(c.top_nodes ?? []).map((n) => (
                  <tr key={`node-${n.node_id}`} className="node-row">
                    <td style={{ paddingLeft: 24, opacity: 0.75 }}>
                      {c.rank}.{n.rank}
                    </td>
                    <td style={{ paddingLeft: 24, opacity: 0.85 }}>↳ {n.host_name}</td>
                    <td>{n.overall_score ?? "—"}</td>
                    <td>{n.snapshot ? `${n.snapshot.available_cpu_cores} cores` : "—"}</td>
                    <td>{n.snapshot ? `${n.snapshot.available_memory_gb} GB` : "—"}</td>
                    <td>{n.projected ? `${n.projected.projected_headroom_percent}%` : "—"}</td>
                    <td />
                  </tr>
                ))}

                {expanded === c.cluster_id && (
                  <tr>
                    <td colSpan={7}>
                      <div className="explain-box">
                        {/* Every rule passed - that is what being here means. Saying
                            it in one line beats ten PASS bullets nobody reads. */}
                        <strong>All {c.rule_results?.length ?? 0} eligibility rules passed.</strong>
                        {c.subscores && (
                          <>
                            <strong style={{ display: "block", marginTop: 8 }}>Score breakdown</strong>
                            <span>
                              capacity={c.subscores.capacity} compatibility={c.subscores.compatibility}{" "}
                              resiliency={c.subscores.resiliency} dependency={c.subscores.dependency}{" "}
                              historical={c.subscores.historical} risk={c.subscores.risk}
                            </span>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}

      {notRecommended.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <button className="secondary" onClick={() => setShowRejected(!showRejected)}>
            {showRejected
              ? "Hide"
              : `Show ${notRecommended.length} cluster(s) that were not recommended`}
          </button>

          {showRejected && (
            <table style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>Cluster</th>
                  {/* The reason, not the rule id. "Why not that one" is answered by
                      what blocked it; the rule id is for the engineer who then asks
                      a second question. */}
                  <th>Why not</th>
                  <th>Free CPU</th>
                  <th>Free RAM</th>
                </tr>
              </thead>
              <tbody>
                {notRecommended.map((c) => (
                  <tr key={c.cluster_id}>
                    <td>{c.cluster_code}</td>
                    <td className="rule-fail">{firstFailure(c) ?? "Did not meet the requirements."}</td>
                    <td>{c.snapshot ? `${c.snapshot.available_cpu_cores} cores` : "—"}</td>
                    <td>{c.snapshot ? `${c.snapshot.available_memory_gb} GB` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </>
  );
}
