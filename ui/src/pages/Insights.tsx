import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { InsightAnswer } from "@/types";

type MessageStatus = "loading" | "completed" | "error";

interface InsightMessage {
  id: string;
  role: "user" | "assistant";
  text?: string;
  status?: MessageStatus;
  answer?: InsightAnswer;
  error?: string;
}

/** Every example is answerable by a different one of the three intents
 *  (app.insights.router) - a reader trying all three sees the shape of what
 *  this screen can do, not just one path through it. */
function buildExamples(): string[] {
  return [
    "How many Sev1 incidents and what are the root causes?",
    "How healthy is our CMDB?",
    "What breaks if atl-03 fails?",
  ];
}

let nextId = 0;
function newId(): string {
  nextId += 1;
  return `insight-msg-${nextId}`;
}

export default function Insights() {
  const [messages, setMessages] = useState<InsightMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const examples = buildExamples();
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function updateMessage(id: string, patch: Partial<InsightMessage>) {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }

  async function send(query: string) {
    const text = query.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    const userMsg: InsightMessage = { id: newId(), role: "user", text };
    const assistantId = newId();
    setMessages((prev) => [...prev, userMsg, { id: assistantId, role: "assistant", status: "loading" }]);
    try {
      const answer = await api.askInsight(text);
      updateMessage(assistantId, { status: "completed", answer });
    } catch (e) {
      updateMessage(assistantId, {
        status: "error",
        // A refused question (unknown dimension, no CI named) and a real
        // failure look the same here on purpose - both are ApiError with a
        // readable detail, and the backend already decided which one this
        // is (400 vs 500). This screen does not re-guess that.
        error: e instanceof ApiError ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  }

  function startNewChat() {
    setMessages([]);
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
      <h2>CMDB Insighter</h2>
      <p className="subtitle">
        Ask about incidents, changes, problems, CMDB health, or what breaks if something fails.
      </p>

      {messages.length > 0 && (
        <div className="chat-toolbar">
          <button className="secondary" disabled={busy} onClick={startNewChat}>
            New question
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
          <InsightBubble key={m.id} message={m} />
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input-bar">
        <textarea
          rows={2}
          placeholder="Ask about incidents, CMDB health, or impact..."
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

function InsightBubble({ message }: { message: InsightMessage }) {
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
        {message.status === "loading" && <span className="chat-typing">Working...</span>}
        {message.status === "error" && <div className="error-box">{message.error}</div>}
        {message.status === "completed" && message.answer && <AnswerView answer={message.answer} />}
      </div>
    </div>
  );
}

/** The headline and narrative lead; caveats sit right beside them (GUARDRAILS:
 *  never a bare number - a filter or a "this is a floor" flag is part of the
 *  answer, not an afterthought). The result table and any secondary findings
 *  (orphan/duplicate counts, raw impact numbers) are real but should not
 *  compete with the headline for attention - folded behind a disclosure, the
 *  same pattern Chat.tsx's Outcome component uses for options not chosen. */
function AnswerView({ answer }: { answer: InsightAnswer }) {
  const [showTable, setShowTable] = useState(false);
  const [showDetails, setShowDetails] = useState(false);
  const hasDetails = answer.details && Object.keys(answer.details).length > 0;

  return (
    <div>
      <strong>{answer.headline}</strong>
      <p>{answer.narrative}</p>

      {answer.insight && <div className="explain-box">{answer.insight}</div>}

      {answer.caveats.length > 0 && (
        <ul>
          {answer.caveats.map((c, i) => (
            <li key={i} className="subtitle">
              {c}
            </li>
          ))}
        </ul>
      )}

      {answer.table && answer.table.rows.length > 0 && (
        <>
          <button className="secondary" style={{ marginTop: 8 }} onClick={() => setShowTable(!showTable)}>
            {showTable ? "Hide" : "Show"} the {answer.table.rows.length}-row breakdown
          </button>
          {showTable && <InsightResultTable table={answer.table} />}
        </>
      )}

      {hasDetails && (
        <>
          <button className="secondary" style={{ marginTop: 8 }} onClick={() => setShowDetails(!showDetails)}>
            {showDetails ? "Hide" : "Show"} details
          </button>
          {showDetails && (
            <pre style={{ marginTop: 6, whiteSpace: "pre-wrap", fontSize: "0.85em" }}>
              {JSON.stringify(answer.details, null, 2)}
            </pre>
          )}
        </>
      )}

      {answer.row_count != null && <div className="chat-meta">{answer.row_count} row(s)</div>}
    </div>
  );
}

function InsightResultTable({ table }: { table: { columns: string[]; rows: (string | number | null)[][] } }) {
  return (
    <table style={{ marginTop: 6 }}>
      <thead>
        <tr>
          {table.columns.map((c) => (
            <th key={c}>{c.replace(/_/g, " ")}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {table.rows.map((row, i) => (
          <tr key={i}>
            {row.map((value, j) => (
              <td key={j}>{value ?? "—"}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
