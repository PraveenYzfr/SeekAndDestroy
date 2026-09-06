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
 *  THE LOOP HAS TO CLOSE, and the first version never did. Reported from
 *  production: after ten clicks both buttons still looked live, choosing a
 *  reason left the panel open as though nothing had happened, and the comment
 *  box had no way to submit - it saved on blur, which is invisible, so the only
 *  way to know anything had been recorded was to guess.
 *
 *  Every path now ENDS somewhere: a rating collapses to a sentence saying what
 *  was recorded, and that sentence carries the way back if the reader wants to
 *  change it. "It stays changeable" was the right instinct and was implemented
 *  as "it never finishes", which is a different thing.
 *
 *  What it still deliberately does not do: demand a reason (rating is one click,
 *  and a thumbs-up with nothing attached is still the data point that matters),
 *  or offer a middle option ("fine, I suppose" is not a signal anybody can act
 *  on, and offering it invites the answer that costs least to give).
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
  //: Open only while the reader is actually answering "what went wrong". Closed
  //: the moment they pick a reason, save a comment, or say they are done.
  const [detailOpen, setDetailOpen] = useState(false);
  //: Fetched from the server, which owns the list. FEEDBACK_REASONS is the
  //: fallback if that call fails - a control with no reasons is worse than a
  //: slightly stale one, and staleness costs a label rather than a rejected
  //: rating, because the ids are validated server-side either way.
  const [reasons, setReasons] = useState<readonly { id: string; label: string }[]>(FEEDBACK_REASONS);

  useEffect(() => {
    let cancelled = false;
    api
      .getFeedbackReasons()
      .then((r) => {
        if (!cancelled && r.reasons?.length) setReasons(r.reasons);
      })
      // Falling back to the bundled list is the point - see the state above.
      .catch(() => undefined);

    // Render in the state this person left it, so a rating already given is not
    // silently invited a second time.
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

  async function send(next: number, nextReason: string | null, nextComment: string,
                      { close }: { close: boolean }) {
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
      setReason(nextReason);
      if (close) setDetailOpen(false);
      // NOT swallowed on the server either: a rating is a deliberate act, and
      // dropping it silently means somebody clicked, saw nothing, and stopped
      // bothering - taking with them the data that would have shown the judge
      // was wrong.
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      // The panel STAYS OPEN on failure. Closing it would report success by
      // disappearing, which is the worst possible acknowledgement of an error.
      setDetailOpen(nextReason !== null || next === -1);
    } finally {
      setBusy(false);
    }
  }

  const reasonLabel = reasons.find((r) => r.id === reason)?.label;

  // ------------------------------------------------------------- recorded
  // The loop closed. One sentence saying what was stored, and the way back.
  if (rating !== null && !detailOpen) {
    return (
      <div className="answer-feedback">
        <span className="feedback-recorded">
          {rating === 1 ? "You marked this useful." : "You marked this not useful."}
          {rating === -1 && reasonLabel ? ` ${reasonLabel}.` : ""}
          {comment.trim() ? " Comment saved." : ""}
        </span>
        <button
          type="button"
          className="secondary feedback-change"
          disabled={busy}
          onClick={() => {
            // Reopening is not a new vote. It shows the detail again with the
            // existing answers in place, so changing one thing does not require
            // re-entering the rest.
            if (rating === -1) setDetailOpen(true);
            else void send(-1, reason, comment, { close: false });
          }}
        >
          {rating === 1 ? "Change to not useful" : "Change"}
        </button>
        {error && <span className="rule-fail">{error}</span>}
      </div>
    );
  }

  // ------------------------------------------------------------ still asking
  return (
    <div className="answer-feedback">
      {rating === null && (
        <>
          <span className="stat-label">Was this useful?</span>
          <button
            type="button"
            className="feedback-vote"
            disabled={busy}
            title="This answer helped"
            onClick={() => void send(1, null, "", { close: true })}
          >
            Yes
          </button>
          <button
            type="button"
            className="feedback-vote"
            disabled={busy}
            title="This answer did not help"
            onClick={() => {
              // Recorded FIRST, then the detail is offered. Somebody who closes
              // the tab without choosing a reason has still been counted, which
              // is the half that matters.
              setDetailOpen(true);
              void send(-1, reason, comment, { close: false });
            }}
          >
            No
          </button>
        </>
      )}

      {detailOpen && (
        <div className="feedback-detail">
          <div className="stat-label">
            Recorded as not useful. What went wrong? (optional)
          </div>
          <div className="feedback-reasons">
            {reasons.map((r) => (
              <button
                type="button"
                key={r.id}
                className={`secondary${reason === r.id ? " chosen" : ""}`}
                disabled={busy}
                // CLOSES on success. Picking a reason is an answer to the
                // question that was asked, so the question should stop being
                // asked - it used to sit there afterwards looking unanswered.
                onClick={() => void send(-1, r.id, comment, { close: true })}
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
          />
          {/* AN EXPLICIT BUTTON, because the box used to save on BLUR. A save
              nobody can see happen is a save nobody trusts happened, and there
              was no way to end the interaction at all. */}
          <div className="feedback-actions">
            <button
              type="button"
              disabled={busy || !comment.trim()}
              onClick={() => void send(-1, reason, comment, { close: true })}
            >
              Save comment
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => setDetailOpen(false)}
            >
              Done
            </button>
          </div>
          {error && <div className="rule-fail">{error}</div>}
        </div>
      )}
    </div>
  );
}
