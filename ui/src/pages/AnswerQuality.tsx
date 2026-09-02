import { useEffect, useState } from "react";

import { api } from "@/api/client";
import type {
  ConversationDetail,
  ConversationSummary,
  GraderScore,
  InvestigationTranscript,
} from "../types";

/**
 * Answer quality, drilled from a list down to the figure that was invented.
 *
 *   grid          every conversation, WORST FIRST
 *   conversation  the session score, and each turn's score
 *   turn          the model calls behind it - prompt, output, verdict
 *
 * WHY THIS IS HERE AND NOT IN GRAFANA. Grafana holds rates over time, from
 * Prometheus counters. This is relational: prompts, responses and stored
 * verdicts in SQL, read by drilling from one row to the next. Putting it there
 * would mean a second datasource and a panel that still could not show a
 * transcript. The two answer different questions - "is quality moving" belongs
 * on a graph, "which figure did it invent, in which call" does not.
 *
 * SORTED WORST FIRST, NOT NEWEST FIRST. The reason to open this screen is to
 * find a bad answer. Ordering by time puts the most recent conversation on top
 * whether or not anything is wrong with it, and buries the one worth reading.
 */
export default function AnswerQuality() {
  const [rows, setRows] = useState<ConversationSummary[]>([]);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [transcript, setTranscript] = useState<InvestigationTranscript | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .getConversations()
      .then((r) => setRows(r.conversations))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  function openConversation(id: string) {
    setTranscript(null);
    setDetail(null);
    api
      .getConversationDetail(id)
      .then(setDetail)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }

  function openTurn(investigationId: number) {
    api
      .getTranscript(investigationId)
      .then(setTranscript)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }

  if (loading) return <p>Loading...</p>;

  return (
    <div>
      <h2>Answer Quality</h2>
      <p style={{ fontSize: 13, opacity: 0.8 }}>
        Every conversation, worst first. Scores come from stored verdicts - nothing is
        graded on the fly, so this shows what was concluded rather than what today's rules
        would say.
      </p>
      {error && <p className="badge rejected">{error}</p>}

      {/* ---- level 1: the grid ---- */}
      <table style={{ marginTop: 10, width: "100%" }}>
        <thead>
          <tr>
            <th>Conversation</th>
            <th>Turns</th>
            <th style={{ textAlign: "right" }}>Number fidelity</th>
            {/* The denominator is part of the claim. 100% over three figures and
                100% over four hundred are different statements. */}
            <th style={{ textAlign: "right" }}>Figures checked</th>
            <th>Last activity</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr
              key={c.conversation_id}
              onClick={() => openConversation(c.conversation_id)}
              style={{
                cursor: "pointer",
                background:
                  detail?.conversation_id === c.conversation_id ? "rgba(255,255,255,0.06)" : undefined,
              }}
            >
              <td style={{ fontFamily: "monospace" }}>{c.conversation_id.slice(0, 12)}...</td>
              <td>{c.turns}</td>
              <td style={{ textAlign: "right" }}>
                <Rate value={c.number_fidelity} />
              </td>
              <td style={{ textAlign: "right", opacity: 0.75 }}>{c.figures_checked}</td>
              <td style={{ opacity: 0.75 }}>{c.last_activity_at?.replace("T", " ").slice(0, 19)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p style={{ fontSize: 13 }}>No conversations recorded yet.</p>}

      {/* ---- level 2: session + turns ---- */}
      {detail && (
        <div className="card" style={{ marginTop: 14 }}>
          <strong>Session</strong>
          <div style={{ fontSize: 12, fontFamily: "monospace", opacity: 0.7 }}>
            {detail.conversation_id}
          </div>
          {detail.note && <p style={{ fontSize: 13 }}>{detail.note}</p>}

          <div style={{ display: "flex", gap: 18, marginTop: 8, flexWrap: "wrap" }}>
            {detail.session.map((g) => (
              <ScoreBlock key={g.grader} score={g} />
            ))}
          </div>
          {detail.session.some((g) => g.mixed_grader_versions) && (
            // A figure spanning two rule sets is a mixture, not one measurement.
            <p style={{ fontSize: 12, marginTop: 6 }} className="badge rejected">
              This conversation spans a grader change - the session figure mixes two rule sets.
            </p>
          )}

          <div style={{ marginTop: 12 }}>
            <strong style={{ fontSize: 13 }}>Turns</strong>
            {detail.turns.map((t) => (
              <div
                key={t.turn_id}
                style={{ borderTop: "1px solid rgba(255,255,255,0.08)", padding: "8px 0" }}
              >
                <div style={{ fontSize: 13 }}>
                  <span style={{ opacity: 0.6 }}>asked:</span> {t.asked || <em>(none recorded)</em>}
                </div>
                <div style={{ fontSize: 13, opacity: 0.85 }}>
                  <span style={{ opacity: 0.6 }}>answered:</span> {t.answered}
                </div>
                <div style={{ marginTop: 4, display: "flex", gap: 14, alignItems: "center" }}>
                  {t.scores.length === 0 ? (
                    // Not the same as scoring zero, and it must not read as "clean".
                    <span className="stat-label">
                      {t.investigation_id == null
                        ? "no investigation - nothing to grade"
                        : "not graded yet"}
                    </span>
                  ) : (
                    t.scores.map((s) => (
                      <span key={s.grader} className="stat-label">
                        {s.grader.replace(/_/g, " ")} <Rate value={s.rate} /> ({s.grounded}/{s.total})
                      </span>
                    ))
                  )}
                  {t.investigation_id != null && (
                    <button
                      className="secondary"
                      style={{ marginLeft: "auto" }}
                      onClick={() => openTurn(t.investigation_id as number)}
                    >
                      Show the model calls
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ---- level 3: the calls ---- */}
      {transcript && (
        <div className="card" style={{ marginTop: 14 }}>
          <strong>Model calls - investigation {transcript.investigation_id}</strong>
          {transcript.note && <p style={{ fontSize: 13 }}>{transcript.note}</p>}
          {transcript.calls.map((c) => (
            <details key={c.audit_id} style={{ marginTop: 8 }}>
              <summary style={{ fontSize: 13 }}>
                {c.schema} &middot; {c.model}{" "}
                {c.grades.map((g) => (
                  <span key={g.grader} className="stat-label" style={{ marginLeft: 8 }}>
                    {g.grader.replace(/_/g, " ")} {g.grounded}/{g.total}
                  </span>
                ))}
              </summary>
              {c.grades
                .filter((g) => g.ungrounded)
                .map((g) => (
                  // The point of storing the tokens: which figure, without a re-run.
                  <p key={g.grader} className="badge rejected" style={{ fontSize: 12 }}>
                    ungrounded in {g.grader.replace(/_/g, " ")}: {g.ungrounded}
                  </p>
                ))}
              <div style={{ fontSize: 11, opacity: 0.75, marginTop: 6 }}>
                graded under {c.grades[0]?.grader_version}
              </div>
              <pre style={{ fontSize: 11, maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap" }}>
                {c.prompt}
              </pre>
              <pre style={{ fontSize: 11, maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap" }}>
                {c.output}
              </pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

/** A rate, or an explicit "not measured". Never a zero standing in for absence. */
function Rate({ value }: { value: number | null }) {
  if (value === null || value === undefined) return <span style={{ opacity: 0.5 }}>-</span>;
  const pct = value * 100;
  return (
    <span className={`badge ${pct >= 99 ? "eligible" : pct >= 90 ? "" : "rejected"}`}>
      {pct.toFixed(1)}%
    </span>
  );
}

function ScoreBlock({ score }: { score: GraderScore }) {
  return (
    <div>
      <div className="stat-label">{score.grader.replace(/_/g, " ")}</div>
      <div style={{ fontSize: 18 }}>
        <Rate value={score.rate} />
      </div>
      <div className="stat-label">
        {score.grounded}/{score.total}
        {score.calls ? ` over ${score.calls} calls` : ""}
      </div>
    </div>
  );
}
