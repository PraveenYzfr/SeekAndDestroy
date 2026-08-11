/**
 * Sign-in state for the browser.
 *
 * The token lives in sessionStorage, not localStorage: it is scoped to the
 * tab and cleared when the tab closes, so a shared or forgotten machine does
 * not leave a usable credential behind. The trade-off is that a new tab
 * requires signing in again, which is the right default for a tool that
 * approves infrastructure changes.
 *
 * The password itself is never stored, never cached, and never leaves the
 * login form - only the returned JWT is kept.
 */

const TOKEN_KEY = "sad.access_token";
const IDENTITY_KEY = "sad.identity";

export interface Identity {
  employee_id: number;
  employee_number: string;
  display_name: string;
}

export interface LoginResponse extends Identity {
  access_token: string;
  token_type: string;
  expires_in_minutes: number;
}

type Listener = (identity: Identity | null) => void;
const listeners = new Set<Listener>();

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function getIdentity(): Identity | null {
  const raw = sessionStorage.getItem(IDENTITY_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Identity;
  } catch {
    return null;
  }
}

export function setSession(response: LoginResponse): void {
  sessionStorage.setItem(TOKEN_KEY, response.access_token);
  const identity: Identity = {
    employee_id: response.employee_id,
    employee_number: response.employee_number,
    display_name: response.display_name,
  };
  sessionStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
  listeners.forEach((l) => l(identity));
}

export function clearSession(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(IDENTITY_KEY);
  listeners.forEach((l) => l(null));
}

/** Subscribe to sign-in/sign-out. Used by App so a 401 anywhere in the app
 *  returns the user to the login screen rather than showing a dead page. */
export function onSessionChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
