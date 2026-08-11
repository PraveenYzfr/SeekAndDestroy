import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { InfrastructureRecommendation, RunInvestigationResult } from "@/types";
import { describeCandidate, isNodeRow } from "@/utils/recommendations";
import { getIdentity } from "@/auth/session";

type MessageStatus = "loading" | "awaiting_review" | "completed" | "error";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text?: string;
  investigationId?: number;
  status?: MessageStatus;
  // Mirrors RunInvestigationResult["review_payload"] - one shape, declared
  // once, so the two cannot drift.
  reviewPayload?: RunInvestigationResult["review_payload"];
  finalReport?: RunInvestigationResult["final_report"];
  recommendations?: InfrastructureRecommendation[];
  error?: string;
}

/** The signed-in employee, not a hardcoded 1. The backend treats the token as
 *  authoritative and rejects a mismatching body value (require_matching_
 *  employee_id), so sending anything else would 403. */
function currentEmployeeId(): number {
  return getIdentity()?.employee_id ?? 0;
}

const EXAMPLES = [
  "Find the best clusters for hosting APP-PAYMENTS.",
  "I need 8 CPU, 32 GB RAM and 500 GB storage for a production Kubernetes workload.",
  "Why was nyc-03 rejected for APP-CRM?",
  "Which clusters are underutilized and could be right-sized?",
];

let nextId = 0;
function newId(): string {
  nextId += 1;
  return `msg-${nextId}`;
}

export default function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function updateMessage(id: string, patch: Partial<ChatMessage>) {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }

  async function applyResult(assistantId: string, result: RunInvestigationResult) {
    if (result.status === "AwaitingReview") {
      updateMessage(assistantId, {
        status: "awaiting_review",
        investigationId: result.investigation_id,
        reviewPayload: result.review_payload,
      });
      return;
    }
    try {
      const { recommendations } = await api.getInvestigationRecommendations(result.investigation_id);
      updateMessage(assistantId, {
        status: "completed",
        investigationId: result.investigation_id,
        finalReport: result.final_report,
        recommendations,
      });
    } catch (e) {
      updateMessage(assistantId, {
        status: "completed",
        investigationId: result.investigation_id,
        finalReport: result.final_report,
      });
    }
  }

  async function send(query: string) {
    const text = query.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    const userMsg: ChatMessage = { id: newId(), role: "user", text };
    const assistantId = newId();
    setMessages((prev) => [...prev, userMsg, { id: assistantId, role: "assistant", status: "loading" }]);
    try {
      const result = await api.createInvestigation(text, currentEmployeeId());
      await applyResult(assistantId, result);
    } catch (e) {
      updateMessage(assistantId, {
        status: "error",
        error: e instanceof ApiError ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }

  async function decide(investigationId: number, decision: "Approve" | "Reject") {
    setBusy(true);
    const followUpId = newId();
    setMessages((prev) => [...prev, { id: followUpId, role: "assistant", status: "loading" }]);
    try {
      const result = await api.resumeInvestigation(investigationId, decision, currentEmployeeId());
      await applyResult(followUpId, result);
    } catch (e) {
      updateMessage(followUpId, {
        status: "error",
        error: e instanceof ApiError ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }

  return (
    <div className="chat-page">
      <h2>Chat</h2>
      <p className="subtitle">
        Ask for hosting recommendations, raw capacity, right-sizing, forecasts or trade-offs in plain
        language. Every answer is backed by the same deterministic engine as the structured screens -
        the AI narrates, it never invents the numbers.
      </p>

      <div className="chat-window">
        {messages.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-title">Try asking:</div>
            {EXAMPLES.map((ex) => (
              <button key={ex} className="secondary chat-example" onClick={() => send(ex)}>
                {ex}
              </button>
            ))}
          </div>
        )}

        {messages.map((m) => (
          <ChatBubble key={m.id} message={m} onDecide={decide} busy={busy} />
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input-bar">
        <textarea
          rows={2}
          placeholder="Ask about hosting, capacity, right-sizing, forecasts..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
        />
        <button disabled={busy || !input.trim()} onClick={() => send(input)}>
          {busy ? "Working..." : "Send"}
        </button>
      </div>
    </div>
  );
}

function ChatBubble({
  message,
  onDecide,
  busy,
}: {
  message: ChatMessage;
  onDecide: (investigationId: number, decision: "Approve" | "Reject") => void;
  busy: boolean;
}) {
  if (message.role === "user") {
    return (
      <div className="chat-message user">
        <div className="chat-bubble">{message.text}</div>
      </div>
    );
  }

  return (
    <div className="chat-message assistant">
      <div className="chat-bubble">
        {message.status === "loading" && <span className="chat-typing">Investigating...</span>}

        {message.status === "error" && <div className="error-box">{message.error}</div>}

        {message.status === "awaiting_review" && message.reviewPayload && message.investigationId != null && (
          <div>
            <p>{message.reviewPayload.message}</p>
            <div className="stat-label">Top candidates</div>
            {/* The reviewer is approving clusters *and* the hosts proposed
                inside them, so both belong in the prompt - showing only the
                cluster codes hid half of what the decision covers. */}
            {message.reviewPayload.top_hosts_by_cluster ? (
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {message.reviewPayload.top_candidates.map((cluster) => (
                  <li key={cluster}>
                    <strong>{cluster}</strong>
                    {" — "}
                    {message.reviewPayload!.top_hosts_by_cluster![cluster]?.length
                      ? message.reviewPayload!.top_hosts_by_cluster![cluster].join(", ")
                      : "no eligible hosts"}
                  </li>
                ))}
              </ul>
            ) : (
              <p>{message.reviewPayload.top_candidates.join(", ")}</p>
            )}
            <div className="chat-review-actions">
              {/* Disabled while any request is in flight: these buttons stay
                  rendered after a decision (the thread keeps its history), so
                  without this a second click re-resumes an already-decided
                  investigation and appends a duplicate answer. */}
              <button disabled={busy} onClick={() => onDecide(message.investigationId!, "Approve")}>
                Approve
              </button>
              <button
                className="danger"
                disabled={busy}
                onClick={() => onDecide(message.investigationId!, "Reject")}
              >
                Reject
              </button>
            </div>
          </div>
        )}

        {message.status === "completed" && (
          <div>
            {message.finalReport ? (
              <>
                <strong>{message.finalReport.title}</strong>
                <p>{message.finalReport.executive_summary}</p>
                {message.finalReport.top_recommendation && (
                  <div className="explain-box">Top recommendation: {message.finalReport.top_recommendation}</div>
                )}
                {message.finalReport.risks.length > 0 && (
                  <>
                    <div className="stat-label">Risks</div>
                    <ul>
                      {message.finalReport.risks.map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  </>
                )}
                {message.finalReport.next_steps.length > 0 && (
                  <>
                    <div className="stat-label">Next steps</div>
                    <ul>
                      {message.finalReport.next_steps.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ul>
                  </>
                )}
              </>
            ) : (
              <p>Investigation #{message.investigationId} completed.</p>
            )}

            {message.recommendations && message.recommendations.length > 0 && (
              <table style={{ marginTop: 10 }}>
                <thead>
                  <tr>
                    <th>Rank</th>
                    <th>Candidate</th>
                    <th>Status</th>
                    <th>Score</th>
                    <th>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {message.recommendations.map((r) => (
                    <tr key={r.RecommendationId}>
                      <td style={isNodeRow(r) ? { paddingLeft: 20, opacity: 0.75 } : undefined}>{r.Rank}</td>
                      <td style={isNodeRow(r) ? { paddingLeft: 20, opacity: 0.85 } : undefined}>
                        {isNodeRow(r) ? "↳ " : ""}
                        {describeCandidate(r)}
                      </td>
                      <td>
                        <span className={`badge ${r.EligibilityStatus === "Eligible" ? "eligible" : "rejected"}`}>
                          {r.EligibilityStatus}
                        </span>
                      </td>
                      <td>{r.OverallScore ?? "—"}</td>
                      <td>{r.EstimatedMonthlyCost != null ? `$${r.EstimatedMonthlyCost}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {message.investigationId != null && (
              <div className="chat-meta">Investigation #{message.investigationId}</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
