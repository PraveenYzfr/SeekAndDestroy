import { Component, type ErrorInfo, type ReactNode } from "react";

/** One bad message must not take the conversation with it.
 *
 *  FOUND BY ACCIDENT, WHICH IS THE POINT. A stub returned a final_report
 *  without its `risks` array. ChatBubble reads `finalReport.risks.length`
 *  unguarded, the read threw, and React unmounted THE ENTIRE TREE - the whole
 *  chat went blank, including every earlier answer that had rendered perfectly
 *  well and the input box needed to ask anything else.
 *
 *  The real service always sends `risks`, even on the "narration unavailable"
 *  fallback path, so that specific crash is latent rather than live. That is
 *  not the reason to fix it. The reason is the BLAST RADIUS: any future shape
 *  change, a truncated response, a proxy returning a partial body, or a field
 *  removed on the server before the client is redeployed, all currently destroy
 *  the whole screen rather than one card.
 *
 *  This is the same principle the backend already applies to itself - a grader
 *  failing must not fail the answer it is grading, and audit writes are
 *  fail-open because losing a log is better than losing an investigation. The
 *  commentary must never be able to destroy the work. The UI had no equivalent.
 *
 *  DELIBERATELY NOT A GLOBAL BOUNDARY. Wrapping the whole app would turn a
 *  broken message into a broken application, which is what already happens. The
 *  boundary belongs at the smallest unit that can fail independently, so
 *  everything else on screen survives.
 */
interface Props {
  children: ReactNode;
  /** Named in the fallback so a report of "one message failed" can be traced to
   *  a specific investigation rather than to "somewhere in the chat". */
  investigationId?: number | null;
}

interface State {
  error: Error | null;
}

export default class MessageBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Logged rather than swallowed. A message that silently renders as an error
    // card is a defect nobody reports, because the screen still looks like it
    // is working.
    console.error("chat message failed to render", {
      investigationId: this.props.investigationId,
      error,
      componentStack: info.componentStack,
    });
  }

  render() {
    if (this.state.error) {
      return (
        <div className="chat-message assistant">
          <div className="chat-bubble">
            <div className="error-box">
              This answer could not be displayed.
              {this.props.investigationId != null && (
                <> Investigation #{this.props.investigationId}.</>
              )}
              <div style={{ fontSize: 12, marginTop: 6, opacity: 0.8 }}>
                The rest of the conversation is unaffected, and the answer itself was
                recorded - this is a display fault, not a lost result.
              </div>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
