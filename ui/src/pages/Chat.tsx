import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { InfrastructureRecommendation, RunInvestigationResult } from "@/types";
import { describeCandidate, isNodeRow } from "@/utils/recommendations";
import { getIdentity } from "@/auth/session";
import ReviewChoice from "@/components/ReviewChoice";
import AnswerFeedbackControl from "@/components/AnswerFeedbackControl";
import MessageBoundary from "@/components/MessageBoundary";

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
  /** The real findings behind finalReport - present whether or not the report
   *  narration itself succeeded, so a failed "write a summary" call never
   *  leaves the reader with nothing but "narration unavailable" when the
   *  actual, already-verified results are sitting right here. */
  recommendationExplanations?: RunInvestigationResult["recommendation_explanations"];
  rejectionPrompt?: RunInvestigationResult["rejection_prompt"];
  recommendations?: InfrastructureRecommendation[];
  error?: string;
  /** Set once this review has been acted on. The bubble stays in the thread as
   *  history - the shortlist is the evidence behind the decision - so without
   *  this its controls remain live and a second click re-decides an
   *  investigation that is already closed. */
  decided?: {
    decision: "Approve" | "Reject" | "RequestMoreAnalysis";
    cluster?: string;
    host?: string;
  };
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
 *
 *  THE SIZES ARE MEASURED, NOT PLAUSIBLE. The first version asked for a Tier-1
 *  workload at 32 cores / 128 GB, which this estate can barely satisfy: it
 *  returned one data centre, sometimes one cluster, and never enough to page
 *  through. A suggested question is the first thing anyone presses, so it
 *  should exercise the product rather than corner it. Counted against
 *  production, by requirement shape:
 *
 *      Tier-1  32c / 128 GB           0-3 eligible, 1-2 DCs   (what it was)
 *      Tier-2  16c /  64 GB / 1 TB      0 eligible
 *      Tier-2   8c /  32 GB / 500 GB    5 eligible, 4 DCs
 *      Tier-3   8c /  32 GB / 500 GB    6 eligible, 5 DCs
 *      Tier-3   4c /  16 GB / 250 GB   11 eligible, 7 DCs
 *
 *  So the first two ask for something the estate can actually place several
 *  ways, which is what makes "show the next 3" and "give me a different data
 *  centre" mean anything when pressed.
 */
function buildExamples(): string[] {
  return [
    "Where can I host a Tier-3 internal web app needing 4 cores, 16 GB RAM and 250 GB storage?",
    "I need 8 cores, 32 GB RAM and 500 GB storage for a Tier-2 production workload.",
    "Which 3 clusters are the best right-sizing candidates?",
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
  // "data center", spelled out - not folded into "in {name}". Real DC names
  // look like "Denver-DC1", where the trailing digit means \bdc\b (the
  // location-word regex app.graph.conversation relies on to know THIS
  // re-scope is about a site, not some other dimension) does not match: "C"
  // and "1" share no word boundary. Spelling it out is what makes this
  // button's click reliably trigger the exclusion the rest of the feature
  // was built for, regardless of what any given DC happens to be named.
  if (c.exclude_data_center) return `Show other options, but not in the ${c.exclude_data_center} data center.`;
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

  /** How many investigations this session has produced.
   *
   *  DERIVED, not counted as it goes: a separate tally would drift from the
   *  messages the moment one failed or was answered without an investigation
   *  behind it. Distinct ids, because a follow-up answered from an earlier
   *  investigation's results carries that same id and is not a new one. */
  const investigationCount = new Set(
    messages.map((m) => m.investigationId).filter((id): id is number => id != null),
  ).size;
  const examples = buildExamples();
  const bottomRef = useRef<HTMLDivElement | null>(null);

  /** The most recent shortlist still awaiting a decision - the only one whose
   *  controls should do anything. Derived rather than stored: a decision, a new
   *  answer or a superseding question all change it, and a second copy of this
   *  fact in state is a copy that goes stale. */
  const liveReviewId = (() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const m = messages[i];
      if (m.status === "awaiting_review" && !m.decided && m.investigationId != null) {
        return m.investigationId;
      }
    }
    return null;
  })();


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
        recommendationExplanations: result.recommendation_explanations,
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
        recommendationExplanations: result.recommendation_explanations,
        rejectionPrompt: result.rejection_prompt,
        recommendations,
      });
    } catch (e) {
      updateMessage(assistantId, {
        status: "completed",
        investigationId: result.investigation_id,
        finalReport: result.final_report,
        recommendationExplanations: result.recommendation_explanations,
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
    decision: "Approve" | "Reject" | "RequestMoreAnalysis",
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
      <h2>Migration Analysis</h2>
      <p className="subtitle">
        Ask about hosting, capacity, right-sizing, forecasts or trade-offs in plain language.
      </p>

      {messages.length > 0 && (
        <div className="chat-toolbar">
          {/* THE SESSION IS THE IDENTITY, AND IT WAS INVISIBLE.
              conversationId has always existed, been tracked, and been the
              thing AnswerQuality groups by - it was just never rendered, held
              in state only to send back with the next request. So the screen
              showed a dozen investigation numbers and nothing tying them
              together, and the obvious reading was that something had split
              one conversation into twelve.
              Nothing had. Each exchange gets its own investigation record on
              purpose - that is what approval and audit hang off - but the
              SESSION is what a person is sitting in, and it belongs at the top
              rather than nowhere. */}
          {conversationId && (
            <div className="chat-session" title={conversationId}>
              <span className="stat-label">Session</span>{" "}
              <span style={{ fontFamily: "monospace" }}>{conversationId.slice(0, 8)}</span>
              {investigationCount > 0 && (
                <span className="stat-label">
                  {" "}· {investigationCount} investigation{investigationCount === 1 ? "" : "s"}
                </span>
              )}
            </div>
          )}
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

        {/* Only the newest open shortlist is live. Older ones stay on screen -
            they are the evidence behind what was asked next - but their
            controls are inert, because acting on a shortlist two questions ago
            resumes an investigation the engineer has moved past, against a
            requirement they have since changed. The panels used to stay fully
            clickable: `busy` went false when the follow-up landed and every
            earlier bubble came back to life. */}
        {messages.map((m) => (
          // ONE BAD MESSAGE MUST NOT TAKE THE CONVERSATION WITH IT. Without
          // this, a single unreadable field in one answer unmounts the whole
          // tree - every earlier answer and the input box included.
          <MessageBoundary key={m.id} investigationId={m.investigationId}>
          <ChatBubble
            message={m}
            onDecide={decide}
            onAsk={send}
            busy={busy}
            conversationId={conversationId}
            superseded={
              m.status === "awaiting_review" &&
              !m.decided &&
              liveReviewId != null &&
              m.investigationId !== liveReviewId
            }
          />
          </MessageBoundary>
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

/** The narrated findings behind final_report, folded behind a disclosure -
 *  same pattern as Outcome's "other options considered". Shown whenever there
 *  is anything to show, not conditioned on whether final_report's own
 *  narration succeeded: when it fails, this is the ONLY place the real,
 *  already-computed, already-verified results are visible at all - the
 *  executive summary above falls back to a generic "narration unavailable"
 *  line, and without this a reader sees that line and nothing else, even
 *  though the actual findings (which cluster, why, what to do) were sitting
 *  in the response the whole time. */
function RecommendationFindings({
  explanations,
}: {
  explanations: NonNullable<RunInvestigationResult["recommendation_explanations"]>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: 8 }}>
      <button className="secondary" onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Show"} {explanations.length} finding{explanations.length === 1 ? "" : "s"}
      </button>
      {open && (
        <div style={{ marginTop: 6 }}>
          {explanations.map((e, i) => {
            // Shape varies by investigation type - a narrated candidate or
            // right-sizing result carries `summary`, a grounded answer
            // carries `answer` instead. Never both, so this picks whichever
            // is actually present rather than assuming one.
            const label = e.cluster_code || e.cluster_or_application_code;
            const text = e.summary ?? e.answer;
            return (
              <div key={i} className="explain-box" style={{ marginTop: i === 0 ? 0 : 8 }}>
                {label && <strong>{label}</strong>}
                {e.classification && <span className="stat-label"> — {e.classification}</span>}
                {text && <p style={{ margin: "4px 0 0" }}>{text}</p>}
                {e.recommended_action && (
                  <p style={{ margin: "4px 0 0", fontSize: 13 }}>Recommended: {e.recommended_action}</p>
                )}
                {e.key_strengths && e.key_strengths.length > 0 && (
                  <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 13 }}>
                    {e.key_strengths.map((s, j) => (
                      <li key={j}>{s}</li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ChatBubble({
  message,
  onDecide,
  onAsk,
  busy,
  superseded,
  conversationId,
}: {
  message: ChatMessage;
  /** Recorded alongside a rating so feedback can be read per conversation as
   *  well as per investigation - "this whole thread was useless" is a different
   *  signal from one bad answer inside a good session. */
  conversationId?: string | null;
  /** A later question has moved past this shortlist. Shown, not removed. */
  superseded?: boolean;
  onDecide: (
    investigationId: number,
    decision: "Approve" | "Reject" | "RequestMoreAnalysis",
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
          <>
            {/* THE OUTCOME SITS ABOVE THE SHORTLIST; IT DOES NOT REPLACE IT.
                This branch used to swap the whole panel for this one line, so
                choosing an option deleted the table the choice was made from -
                the figures behind a placement disappeared at the moment the
                placement became a decision worth defending. */}
            {message.decided && (
              <div className="review-decided" style={{ marginBottom: 8 }}>
                {message.decided.decision === "Approve" ? (
                  <>
                    <span className="badge eligible">Selected</span>{" "}
                    <strong>{message.decided.cluster}</strong>
                    {message.decided.host && <> / <strong>{message.decided.host}</strong></>}
                  </>
                ) : (
                  <>
                    <span className="badge rejected">Moved on</span> from this shortlist
                  </>
                )}
              </div>
            )}
            <ReviewChoice
              payload={message.reviewPayload}
              investigationId={message.investigationId}
              busy={busy}
              locked={Boolean(message.decided) || Boolean(superseded)}
              lockedNote={
                message.decided
                  ? "Decided - kept here as the evidence behind it."
                  : superseded
                    ? "Superseded by a later question - kept for reference."
                    : undefined
              }
              onDecide={onDecide}
            />
          </>
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
                {(message.finalReport.risks?.length ?? 0) > 0 && (
                  <>
                    <div className="stat-label">Risks</div>
                    <ul>
                      {message.finalReport.risks.map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  </>
                )}
                {(message.finalReport.next_steps?.length ?? 0) > 0 && (
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

            {message.recommendationExplanations && message.recommendationExplanations.length > 0 && (
              <RecommendationFindings explanations={message.recommendationExplanations} />
            )}

            {message.recommendations && message.recommendations.length > 0 && (
              <Outcome recommendations={message.recommendations} />
            )}

            {/* THE ONLY HUMAN GROUND TRUTH THIS PLATFORM HAS, and until now it
                was unreachable: the table, repository, endpoints and tests all
                existed with no control and no gateway route.

                Placed at the END of a completed answer rather than beside the
                status line, because "was this useful" can only be answered by
                somebody who has read the thing. */}
            {message.investigationId != null && message.status === "completed" && (
              <AnswerFeedbackControl
                investigationId={message.investigationId}
                conversationId={conversationId}
              />
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
