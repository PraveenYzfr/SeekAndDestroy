import type {
  AnswerFeedback,
  ConversationDetail,
  ConversationSummary,
  EvaluationResult,
  InvestigationTranscript,
  ModelProvider,
  ModelRole,
} from "../types";
import type {
  CmdbApplication,
  InfrastructureCluster,
  HostingRecommendationResponse,
  ClusterRightSizingResult,
  ConsolidationCandidate,
  ClusterForecast,
  Investigation,
  InfrastructureRecommendation,
  RunInvestigationResult,
  InsightAnswer,
} from "@/types";

import { clearSession, getToken, setSession, type LoginResponse } from "@/auth/session";

const BASE = "/api";

class ApiError extends Error {
  constructor(
    public status: number,
    public title: string,
    public detail: string,
  ) {
    super(`${title}: ${detail}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      // Every gateway controller except /api/auth/* is [Authorize]. Without
      // this header the whole app 401s, which is exactly what it used to do.
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    // An expired or revoked token must return the user to the login screen
    // rather than leaving them clicking a dead UI. Tokens are short-lived
    // (60 min by default), so this fires in normal use, not just on attack.
    if (response.status === 401 && !path.startsWith("/auth/")) {
      clearSession();
    }
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.title ?? response.statusText, body.detail ?? "Request failed.");
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  /** Username is the employee number (E1001) or the email address. The
   *  password is passed straight through and never stored anywhere. */
  login: async (username: string, password: string): Promise<LoginResponse> => {
    const result = await request<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    setSession(result);
    return result;
  },

  // ---- admin: model roles ------------------------------------------------
  // 403 rather than a hidden route when the caller is not an administrator -
  // the API decides, the screen only reports what it was told.
  getModelRoles: () => request<{ roles: ModelRole[]; evaluation_note: string }>("/admin/model-roles"),

  /** refresh=true re-asks every provider instead of using the 10-minute cache.
   *  Worth offering explicitly: a model retired minutes ago is the case where
   *  the cached list is exactly wrong. */
  getModelProviders: (refresh = false) =>
    request<{ providers: ModelProvider[] }>(`/admin/model-providers${refresh ? "?refresh=true" : ""}`),

  /** Grades calls that already happened, from sad.AgentAuditLog. Calls no model
   *  and spends nothing, which is why an admin screen can run it on demand. */
  getEvaluation: (limit = 5000) =>
    request<EvaluationResult>(`/admin/evaluation?limit=${limit}`),

  /** Conversations to inspect, WORST FIRST - the reason to open this list is to
   *  find a bad answer, not the newest one. */
  getConversations: (limit = 50) =>
    request<{ conversations: ConversationSummary[] }>(`/admin/conversations?limit=${limit}`),

  /** One conversation at all three levels: session, turn, and the calls behind
   *  a turn (via investigation_id). */
  getConversationDetail: (conversationId: string) =>
    request<ConversationDetail>(`/admin/conversations/${conversationId}`),

  /** Every model call in one turn - prompt, output, model - with its stored
   *  grade. Reads recorded verdicts; grades nothing on the fly. */
  getTranscript: (investigationId: number) =>
    request<InvestigationTranscript>(`/admin/investigations/${investigationId}/transcript`),

  /** Returns the STORED row, not the submitted one - so the screen can apply
   *  it to its own state instead of re-fetching every role, without inventing
   *  a "set by" or a timestamp the server never recorded. */
  setModelRole: (role: string, provider: string, model: string) =>
    request<{
      role: string;
      provider: string;
      model: string;
      source: ModelRole["source"];
      updated_by: string | null;
      updated_at: string | null;
      unverified: boolean;
    }>(
      `/admin/model-roles/${role}`,
      { method: "PUT", body: JSON.stringify({ provider, model }) },
    ),

  /** Carries what the role resolves to NOW that the override is gone, so the
   *  screen need not re-fetch to find out - and so it shows the real reason
   *  ("tier", "judge_default") rather than assuming "config". */
  clearModelRole: (role: string) =>
    request<{
      role: string;
      removed: boolean;
      provider: string;
      model: string;
      source: ModelRole["source"];
    }>(`/admin/model-roles/${role}`, { method: "DELETE" }),

  /** THE ONLY HUMAN GROUND TRUTH IN THIS PLATFORM. Everything else - fidelity,
   *  completeness, the judge - is a machine's opinion of a machine's work.
   *
   *  No employee id in the payload: the AI service takes it from the token, so
   *  nobody can rate as somebody else. */
  submitAnswerFeedback: (
    investigationId: number,
    body: { rating: number; reason?: string | null; comment?: string | null; conversationId?: string | null },
  ) =>
    request<{ investigation_id: number; rating: number; recorded: boolean }>(
      `/investigations/${investigationId}/feedback`,
      {
        method: "POST",
        body: JSON.stringify({
          rating: body.rating,
          reason: body.reason ?? null,
          comment: body.comment ?? null,
          conversationId: body.conversationId ?? null,
        }),
      },
    ),

  /** This person's own rating, so the control renders in the state they left
   *  rather than resetting and inviting a second, contradictory vote. */
  getMyAnswerFeedback: (investigationId: number) =>
    request<AnswerFeedback>(`/investigations/${investigationId}/feedback`),

  getApplications: (environment?: string) =>
    request<CmdbApplication[]>(`/cmdb/applications${environment ? `?environment=${environment}` : ""}`),

  getClusters: (environment?: string) =>
    request<InfrastructureCluster[]>(`/cmdb/clusters${environment ? `?environment=${environment}` : ""}`),

  getCluster: (clusterId: number) => request<InfrastructureCluster>(`/cmdb/clusters/${clusterId}`),

  /** explain adds narration to the response. Left off by default because it
   *  costs a model call; the screens ask for it only when the reader clicks. */
  getHostingRecommendations: (applicationCode: string, explain = false) =>
    request<HostingRecommendationResponse>("/recommendations/hosting", {
      method: "POST",
      body: JSON.stringify({ applicationCode, explain }),
    }),

  getCapacityRecommendations: (payload: Record<string, unknown>) =>
    request<HostingRecommendationResponse>("/recommendations/capacity", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getClusterRightSizing: (clusterCode?: string) =>
    request<{ results: ClusterRightSizingResult[] }>("/recommendations/right-sizing", {
      method: "POST",
      body: JSON.stringify({ clusterCode: clusterCode ?? null }),
    }),

  getApplicationRightSizing: (applicationCode: string) =>
    request<{ results: unknown[] }>("/recommendations/right-sizing", {
      method: "POST",
      body: JSON.stringify({ applicationCode }),
    }),

  analyzeConsolidation: (environment?: string) =>
    request<{ feasible_count: number; results: ConsolidationCandidate[] }>("/recommendations/consolidation", {
      method: "POST",
      body: JSON.stringify({ environment: environment ?? null }),
    }),

  getForecast: (clusterCode: string, horizonDays: number, explain = false) =>
    request<ClusterForecast>("/recommendations/forecast", {
      method: "POST",
      body: JSON.stringify({ clusterCode, horizonDays, explain }),
    }),

  approveRecommendation: (recommendationId: number, reviewerEmployeeId: number, reason?: string) =>
    request(`/recommendations/${recommendationId}/approve`, {
      method: "POST",
      body: JSON.stringify({ reviewerEmployeeId, reason }),
    }),

  rejectRecommendation: (recommendationId: number, reviewerEmployeeId: number, reason?: string) =>
    request(`/recommendations/${recommendationId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reviewerEmployeeId, reason }),
    }),

  /** conversationId threads follow-ups together. Omit it to start a new
   *  conversation - the server generates the id and returns it on every
   *  response, and it must belong to the signed-in employee or the request is
   *  rejected, so it cannot be claimed by guessing. */
  createInvestigation: (query: string, createdByEmployeeId: number, conversationId?: string | null) =>
    request<RunInvestigationResult>("/investigations", {
      method: "POST",
      body: JSON.stringify({ query, createdByEmployeeId, conversationId: conversationId ?? undefined }),
    }),

  getInvestigation: (investigationId: number) => request<Investigation>(`/investigations/${investigationId}`),

  /** selectedClusterCode/selectedHostName name the option chosen. Approving
   *  without them leaves everything PendingReview rather than approving the
   *  whole shortlist. */
  resumeInvestigation: (
    investigationId: number,
    decision: string,
    reviewerEmployeeId: number,
    comments?: string,
    selectedClusterCode?: string,
    selectedHostName?: string,
  ) =>
    request<RunInvestigationResult>(`/investigations/${investigationId}/resume`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        reviewerEmployeeId,
        comments,
        selectedClusterCode,
        selectedHostName,
      }),
    }),

  getInvestigationRecommendations: (investigationId: number) =>
    request<{ investigation: Investigation; recommendations: InfrastructureRecommendation[] }>(
      `/investigations/${investigationId}/recommendations`,
    ),

  /** CMDB Insighter: one free-text question, one composed answer. No
   *  conversation threading yet - each question is independent, unlike
   *  /investigations. */
  askInsight: (query: string) =>
    request<InsightAnswer>("/insights/ask", {
      method: "POST",
      body: JSON.stringify({ query }),
    }),
};

export { ApiError };
