import { useState } from "react";
import { api, ApiError } from "@/api/client";

/**
 * Sign-in screen. Shown whenever there is no valid session - including after
 * a 401, so an expired token returns here instead of leaving a dead UI.
 *
 * The password only ever exists in this component's state and in the request
 * body. It is never stored, never put in a URL, and never logged.
 */
export default function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      setPassword("");
      onSignedIn();
    } catch (e) {
      // The API deliberately returns one message for every failure so this
      // screen cannot be used to discover which accounts exist.
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 380, margin: "12vh auto" }}>
      <h2 style={{ marginBottom: 4 }}>SeekAndDestroy</h2>
      <p className="subtitle" style={{ marginTop: 0 }}>
        Sign in to run investigations and approve recommendations.
      </p>

      <form className="card" onSubmit={submit}>
        <div className="form-row">
          <label htmlFor="username">Employee number or email</label>
          <input
            id="username"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="form-row" style={{ marginTop: 10 }}>
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        {error && (
          <div className="error-box" style={{ marginTop: 12 }}>
            {error}
          </div>
        )}

        <button type="submit" disabled={busy || !username || !password} style={{ marginTop: 14 }}>
          {busy ? "Signing in..." : "Sign in"}
        </button>
      </form>

      <p className="stat-label" style={{ textAlign: "center" }}>
        Trouble signing in? Contact your administrator.
      </p>
    </div>
  );
}
