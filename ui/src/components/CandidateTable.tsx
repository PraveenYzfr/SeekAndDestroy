import { Fragment, useState } from "react";
import type { CandidateScore } from "@/types";

export default function CandidateTable({ candidates }: { candidates: CandidateScore[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);

  return (
    <table>
      <thead>
        <tr>
          <th>Rank</th>
          <th>Cluster</th>
          <th>Status</th>
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
        {candidates.map((c) => (
          <Fragment key={c.cluster_id}>
            <tr>
              <td>{c.rank}</td>
              <td>
                <strong>{c.cluster_code}</strong>
              </td>
              <td>
                <span className={`badge ${c.eligibility_status === "Eligible" ? "eligible" : "rejected"}`}>
                  {c.eligibility_status}
                </span>
              </td>
              <td>{c.overall_score ?? "—"}</td>
              <td>{c.snapshot ? `${c.snapshot.available_cpu_cores} cores` : "—"}</td>
              <td>{c.snapshot ? `${c.snapshot.available_memory_gb} GB` : "—"}</td>
              <td>{c.projected ? `${c.projected.projected_headroom_percent}%` : "—"}</td>
              <td>
                <button className="secondary" onClick={() => setExpanded(expanded === c.cluster_id ? null : c.cluster_id)}>
                  {expanded === c.cluster_id ? "Hide" : "Detail"}
                </button>
              </td>
            </tr>
            {/* Recommended hosts inside this cluster. Their scores are only
                comparable to their siblings, never to the cluster row above -
                the node capacity sub-score uses a different scale (see
                docs/scoring-model.md). */}
            {(c.top_nodes ?? []).map((n) => (
              <tr key={`node-${n.node_id}`} className="node-row">
                <td style={{ paddingLeft: 24, opacity: 0.75 }}>{c.rank}.{n.rank}</td>
                <td style={{ paddingLeft: 24, opacity: 0.85 }}>↳ {n.host_name}</td>
                <td>
                  <span className={`badge ${n.eligibility_status === "Eligible" ? "eligible" : "rejected"}`}>
                    {n.eligibility_status}
                  </span>
                </td>
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
                    <strong>Rule evaluation</strong>
                    <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                      {c.rule_results.map((r) => (
                        <li key={r.rule_id} className={r.passed ? "rule-pass" : "rule-fail"}>
                          {r.passed ? "PASS" : "FAIL"} {r.rule_id}: {r.reason}
                        </li>
                      ))}
                    </ul>
                    {c.subscores && (
                      <>
                        <strong style={{ display: "block", marginTop: 8 }}>Sub-scores</strong>
                        <span>
                          capacity={c.subscores.capacity} compatibility={c.subscores.compatibility} resiliency=
                          {c.subscores.resiliency} dependency={c.subscores.dependency}{" "}
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
  );
}
