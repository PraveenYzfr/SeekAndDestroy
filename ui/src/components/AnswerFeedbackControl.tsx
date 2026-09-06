import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { FEEDBACK_REASONS } from "@/types";

/** Was this answer any use?
 *
 *  THE ONLY HUMAN GROUND TRUTH IN THIS PLATFORM. Number fidelity is arithmetic
 *  over evidence, completeness is field presence, and the judge is one model's
 *  opinion of another model's work. Not one of them has ever been checked
 *  against a person who actually needed the answer.
 *
 *  The table, the repository, the endpoints and their tests all existed before
 *  this file did. There was no control and no gateway route, so nobody could
 *  submit a rating and the only column that could settle "is the judge worth
 *  what it costs" stayed empty.
 *
 *  THREE THINGS THIS DELIBERATELY DOES NOT DO:
 *
 *  - It does not demand a reason. Rating is one click; the reason and comment
 *    appear afterwards and can be ignored. Requiring an explanation is how a
 *    feedback control stops being used, and a thumbs-up with nothing attached
 *    is still the data point that matters.
 *  - It does not offer a middle option. "Fine, I suppose" is not a signal
 *    anybody can act on, and its presence invites the answer that costs least
 *    to give. Helpful or not helpful, and the option to say nothing at all.
 *  - It does not disappear once used. The rating stays visible and changeable,
 *    because the moment somebody most wants to revise a verdict is after they
 *    have tried to act on the answer.
 */
export default function AnswerFeedbackControl({
  investigationId,
  conversationId,
}: {
  investigationId: number;
  conversationId?: string | null;
}) {
  const [rating, setRating] = useState<number | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  //: Shown only after a negative rating, and only until it is sent. Asking
  //: what was wrong is useful; asking before knowing whether anything was is
  //: an interrogation.
  const [detailOpen, setDetailOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  //: Fetched from the server, which owns the list. FEEDBACK_REASONS is the
  //: fallback if that call fails - a control with no reasons is worse than a
  //: slightly stale one, and staleness costs a label rather than a rejected
  //: rating, because the ids are validated server-side either way.
  const [reasons, setReasons] = useState<readonly { id: string; label: string }[]>(FEEDBACK_REASONS);

  useEffect(() => {
    let cancelled = false;
    // Render in the state this person left it. Without this the control resets
    // on every re-render and invites a second, contradictory vote from someone
    // who already voted.
    api
      .getFeedbackReasons()
      .then((r) => {
        if (!cancelled && r.reasons?.length) setReasons(r.reasons);
      })
      // Falling back to the bundled list is the point - see the state above.
      .catch(() => undefined);

    api
      .getMyAnswerFeedback(investigationId)
      .then((existing) => {
        if (cancelled) return;
        setRating(existing.rating);
        setReason(existing.reason ?? null);
        setComment(existing.comment ?? "");
      })
      // A missing prior rating is the normal case, not an error worth showing.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [investigationId]);

  async function send(next: number, nextReason: string | null, nextComment: string) {
    setBusy(true);
    setError("");
    try {
      await api.submitAnswerFeedback(investigationId, {
        rating: next,
        reason: nextReason,
        comment: nextComment.trim() || null,
        conversationId,
      });
      setRating(next);
      setSaved(true);
      // NOT swallowed on the server either: a rating is a deliberate act, and
      // dropping it silently means somebody clicked, saw nothing, and stopped
      // bothering - taking with them the data that would have shown the judge
      // was wrong.
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="answer-feedback">
      <span className="stat-label">Was this useful?</span>

      <button
        type="button"
        className={`feedback-vote${rating === 1 ? " chosen" : ""}`}
        disabled={busy}
        aria-pressed={rating === 1}
        title="This answer helped"
        onClick={() => {
          setDetailOpen(false);
          void send(1, null, "");
        }}
      >
        Yes
      </button>

      <button
        type="button"
        className={`feedback-vote${rating === -1 ? " chosen" : ""}`}
        disabled={busy}
        aria-pressed={rating === -1}
        title="This answer did not help"
        onClick={() => {
          // Recorded FIRST, then the detail is offered. If the person closes
          // the tab without picking a reason, the rating still counted - which
          // is the half that matters.
          setDetailOpen(true);
          void send(-1, reason, comment);
        }}
      >
        No
      </button>

      {/* Reserved rather than conditional: this line appearing would push the
          next message down at the moment of clicking, which is the same jitter
          the Model Settings screen had. */}
      <span className="feedback-status" aria-live="polite">
        {error ? <span className="rule-fail">{error}</span> : saved ? "Thanks - recorded." : ""}
      </span>

      {detailOpen && rating === -1 && (
        <div className="feedback-detail">
          <div className="stat-label">What went wrong? (optional)</div>
          <div className="feedback-reasons">
            {reasons.map((r) => (
              <button
                type="button"
                key={r.id}
                className={`secondary${reason === r.id ? " chosen" : ""}`}
                disabled={busy}
                onClick={() => {
                  setReason(r.id);
                  void send(-1, r.id, comment);
                }}
              >
                {r.label}
              </button>
            ))}
          </div>
          <textarea
            rows={2}
            placeholder="Anything else worth knowing (optional)"
            value={comment}
            disabled={busy}
            onChange={(e) => setComment(e.target.value)}
            onBlur={() => {
              if (comment.trim()) void send(-1, reason, comment);
            }}
          />
        </div>
      )}
    </div>
  );
}
