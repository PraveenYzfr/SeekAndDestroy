import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { InfrastructureRecommendation, RunInvestigationResult } from "@/types";
import { describeCandidate, isNodeRow } from "@/utils/recommendations";
import { getIdentity } from "@/auth/session";
import ReviewChoice from "@/components/ReviewChoice";

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
  rejectionPrompt?: RunInvestigationResult["rejection_prompt"];
  recommendations?: InfrastructureRecommendation[];
  error?: string;
  /** Set once this review has been acted on. The bubble stays in the thread as
   *  history, so without this its Approve/Reject controls remain live and a
   *  second click re-decides an investigation that is already closed. */
  decided?: { decision: "Approve" | "Reject"; cluster?: string; host?: string };
}

/** The signed-in employee, not a hardcoded 1. The backend treats the token as
 *  authoritative and rejects a mismatching body value (require_matching_
 *  employee_id), so sending anything else would 403. */
function currentEmployeeId(): number {
  return getIdentity()?.employee_id ?? 0;
}

/** Prompts built from whatever is actually in the CMDB, not a fixed menu.
 *
 *  Hardcoding "Find the best clusters for hosting APP-PAYMENTS" made the thing
 *  look like a scripted demo - it implied the system knows about one blessed
 *  application.
 *
 *  Reading a real application code from the estate was the fix for that and was
 *  itself wrong: apps[0] is the same row on every load, so the suggestion was
 *  "Find the best clusters for hosting APP-AML-API0044" every single time - one
 *  blessed application again, just chosen by the database instead of by us.
 *
 *  Worse, it was the wrong SHAPE. An application that already has a code is
 *  already hosted, so where it should go is a question nobody has. Somebody
 *  asking where to place something is placing something new and describes it by
 *  tier, platform and size. These examples do that, name nothing that has to
 *  exist, and need no request to build.
 */
function buildExamples(): string[] {
  return [
    "Where can I host a Tier-1 production Java app needing 32 cores and 128 GB?",
    "I need 64 cores, 512 GB RAM and 4 TB storage for a production Kubernetes workload.",
    "Which clusters are underutilized and could be right-sized?",
  ];
}

let nextId = 0;
function newId(): string {
  nextId += 1;
  return `msg-${nextId}`;
}

/** Turn a picked rejection reason into the next question.
 *
 *  Deliberately a normal chat message rather than a new constraint API. The
 *  conversation already carries the shortlist it is refining, so this reuses the
 *  path that "show me the next three" uses instead of adding a second way to
 *  narrow a search - two mechanisms would drift, and the one used less often is
 *  the one that rots.
 */
function refineFromRejection(
  option: { id: string; constraint: Record<string, unknown> },
  rejectedCluster?: string | null,
): string {
  const c = option.constraint;
  if (c.exclude_data_center) return `Show other options, but not in ${c.exclude_data_center}.`;
  if (c.min_headroom_percent) return `Show options with at least ${c.min_headroom_percent}% headroom after the move.`;
  if (c.min_resiliency) return "Show options with better failure-domain separation.";
  if (c.min_change_risk) return "Show options on clusters with less change activity.";
  return `Show other options, but not ${rejectedCluster ?? "that cluster"}.`;
}


export default function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  /** The chat this thread belongs to. Null until the first answer comes back:
   *  the server generates the id, never the client, and every later message
   *  sends it so "give me the options again" has a referent. Deliberately not
   *  persisted across reloads - a reload starts a fresh conversation, which is
   *  honest, because the message history on screen is gone too. */
  const [conversationId, setConversationId] = useState<string | null>(null);
  const examples = buildExamples();
  const bottomRef = useRef<HTMLDivElement | null>(null);


  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function updateMessage(id: string, patch: Partial<ChatMessage>) {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }

  async function applyResult(assistantId: string, result: RunInvestigationResult) {
    if (result.conversation_id) setConversationId(result.conversation_id);
    if (result.status === "AwaitingReview") {
      updateMessage(assistantId, {
        status: "awaiting_review",
        investigationId: result.investigation_id,
        reviewPayload: result.review_payload,
      });
      return;
    }
    // A greeting, a request too vague to act on, or a follow-up with nothing
    // to refer to: answered directly, with no Investigation row behind it and
    // so no recommendations to fetch.
    if (result.investigation_id == null) {
      updateMessage(assistantId, {
        status: "completed",
        finalReport: result.final_report,
        rejectionPrompt: result.rejection_prompt,
      });
      return;
    }
    try {
      const { recommendations } = await api.getInvestigationRecommendations(result.investigation_id);
      updateMessage(assistantId, {
        status: "completed",
        investigationId: result.investigation_id,
        finalReport: result.final_report,
        rejectionPrompt: result.rejection_prompt,
        recommendations,
      });
    } catch (e) {
      updateMessage(assistantId, {
        status: "completed",
        investigationId: result.investigation_id,
        finalReport: result.final_report,
        rejectionPrompt: result.rejection_prompt,
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
      const result = await api.createInvestigation(text, currentEmployeeId(), conversationId);
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

  async function decide(
    investigationId: number,
    decision: "Approve" | "Reject",
    selectedClusterCode?: string,
    selectedHostName?: string,
  ) {
    // An investigation is decided once. Marking the bubble as decided closes
    // its controls, but that is presentation - it does not stop a second call
    // reaching the API. A stale panel, a double click, or a resume that comes
    // back AwaitingReview again all produce live buttons for an investigation
    // that already has a decision recorded against it.
    if (messages.some((m) => m.investigationId === investigationId && m.decided)) return;

    setBusy(true);
    const followUpId = newId();
    // Close the review bubble first: it stays in the thread as history, and
    // leaving its controls live invites a second decision on a closed
    // investigation.
    setMessages((prev) =>
      prev.map((m) =>
        m.investigationId === investigationId && m.status === "awaiting_review"
          ? { ...m, decided: { decision, cluster: selectedClusterCode, host: selectedHostName } }
          : m,
      ),
    );
    setMessages((prev) => [...prev, { id: followUpId, role: "assistant", status: "loading" }]);
    try {
      const result = await api.resumeInvestigation(
        investigationId, decision, currentEmployeeId(), undefined, selectedClusterCode, selectedHostName,
      );
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

  function startNewChat() {
    setMessages([]);
    setConversationId(null);
    setInput("");
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
        Ask about hosting, capacity, right-sizing, forecasts or trade-offs in plain language.
      </p>

      {messages.length > 0 && (
        <div className="chat-toolbar">
          {/* Starting a new chat is a real action, not cosmetic: it drops the
              conversation id, so the next message is resolved against nothing
              rather than against a shortlist the engineer has moved on from. */}
          <button className="secondary" disabled={busy} onClick={startNewChat}>
            New chat
          </button>
        </div>
      )}

      <div className="chat-window">
        {messages.length === 0 && (
          <div className="chat-empty">
            <div className="chat-empty-title">Try asking:</div>
            {examples.map((ex) => (
              <button key={ex} className="secondary chat-example" onClick={() => send(ex)}>
                {ex}
              </button>
            ))}
          </div>
        )}

        {messages.map((m) => (
          <ChatBubble key={m.id} message={m} onDecide={decide} onAsk={send} busy={busy} />
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

/**
 * What was decided, then everything else folded away.
 *
 * Listing all twelve rows flat made the outcome invisible: the one placement
 * that was actually approved sat among eleven that were not, in the same
 * weight, and the reader had to hunt for it. The rest are kept - "what else
 * was considered" is real evidence - just not competing with the answer.
 */
function Outcome({ recommendations }: { recommendations: InfrastructureRecommendation[] }) {
  const [showOthers, setShowOthers] = useState(false);
  const chosen = recommendations.filter((r) => r.Status === "Approved");
  const others = recommendations.filter((r) => r.Status !== "Approved");

  if (chosen.length === 0) {
    // Rejected, or approved without picking an option - nothing was selected,
    // so there is no outcome to lead with.
    return <RecommendationRows rows={recommendations} />;
  }

  return (
    <div style={{ marginTop: 10 }}>
      <div className="stat-label">Approved</div>
      <RecommendationRows rows={chosen} />
      {others.length > 0 && (
        <>
          <button className="secondary" style={{ marginTop: 8 }} onClick={() => setShowOthers(!showOthers)}>
            {showOthers ? "Hide" : `Show ${others.length} other options considered`}
          </button>
          {showOthers && <RecommendationRows rows={others} muted />}
        </>
      )}
    </div>
  );
}

function RecommendationRows({
  rows,
  muted = false,
}: {
  rows: InfrastructureRecommendation[];
  muted?: boolean;
}) {
  return (
    <table style={{ marginTop: 6, opacity: muted ? 0.7 : 1 }}>
      <thead>
        <tr>
          <th>Rank</th>
          <th>Candidate</th>
          <th>Outcome</th>
          <th>Score</th>
          <th>Headroom</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.RecommendationId}>
            <td style={isNodeRow(r) ? { paddingLeft: 20, opacity: 0.75 } : undefined}>{r.Rank}</td>
            <td style={isNodeRow(r) ? { paddingLeft: 20, opacity: 0.85 } : undefined}>
              {isNodeRow(r) ? "↳ " : ""}
              {describeCandidate(r)}
            </td>
            <td>
              {/* Status, not EligibilityStatus: after a decision the question
                  is what happened to this option, not whether it qualified. */}
              <span className={`badge ${r.Status === "Approved" ? "eligible" : "rejected"}`}>{r.Status}</span>
            </td>
            <td>{r.OverallScore ?? "—"}</td>
            <td>{r.ProjectedHeadroomPercent != null ? `${r.ProjectedHeadroomPercent}%` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ChatBubble({
  message,
  onDecide,
  onAsk,
  busy,
}: {
  message: ChatMessage;
  onDecide: (
    investigationId: number,
    decision: "Approve" | "Reject",
    selectedClusterCode?: string,
    selectedHostName?: string,
  ) => void;
  /** Sends a follow-up question as if the engineer had typed it. Used by the
   *  rejection prompt, so picking a reason continues the same conversation
   *  rather than starting a parallel one. */
  onAsk: (query: string) => void;
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
          message.decided ? (
            <div className="review-decided">
              {message.decided.decision === "Approve" ? (
                <>
                  <span className="badge eligible">Approved</span>{" "}
                  <strong>{message.decided.cluster}</strong>
                  {message.decided.host && <> / <strong>{message.decided.host}</strong></>}
                </>
              ) : (
                <>
                  <span className="badge rejected">Rejected</span> the whole shortlist
                </>
              )}
            </div>
          ) : (
            <ReviewChoice
              payload={message.reviewPayload}
              investigationId={message.investigationId}
              busy={busy}
              decided={Boolean(message.decided)}
              onDecide={onDecide}
            />
          )
        )}

        {/* A rejection asks rather than narrates. Rendered BEFORE the report
            check and returning early, because the two are alternatives: the
            backend sets one or the other, and a reviewer who has just said no
            should not be handed an executive summary of what they declined.

            The options are buttons rather than prose because the model was not
            asked to invent them - they are derived from the rejected
            candidate's own figures, so they can be pressed. */}
        {message.status === "completed" && message.rejectionPrompt ? (
          <div>
            <p>{message.rejectionPrompt.question}</p>
            <div className="grid" style={{ gap: 6 }}>
              {message.rejectionPrompt.options.map((option) => (
                <button
                  key={option.id}
                  className="secondary"
                  disabled={busy}
                  onClick={() => onAsk(refineFromRejection(option, message.rejectionPrompt?.rejected_cluster))}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>
        ) : message.status === "completed" && (
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
              <Outcome recommendations={message.recommendations} />
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
