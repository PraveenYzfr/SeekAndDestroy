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

  getApplications: (environment?: string) =>
    request<CmdbApplication[]>(`/cmdb/applications${environment ? `?environment=${environment}` : ""}`),

  getClusters: (environment?: string) =>
    request<InfrastructureCluster[]>(`/cmdb/clusters${environment ? `?environment=${environment}` : ""}`),

  getCluster: (clusterId: number) => request<InfrastructureCluster>(`/cmdb/clusters/${clusterId}`),

  getHostingRecommendations: (applicationCode: string) =>
    request<HostingRecommendationResponse>("/recommendations/hosting", {
      method: "POST",
      body: JSON.stringify({ applicationCode }),
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

  getForecast: (clusterCode: string, horizonDays: number) =>
    request<ClusterForecast>("/recommendations/forecast", {
      method: "POST",
      body: JSON.stringify({ clusterCode, horizonDays }),
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

  createInvestigation: (query: string, createdByEmployeeId: number) =>
    request<RunInvestigationResult>("/investigations", {
      method: "POST",
      body: JSON.stringify({ query, createdByEmployeeId }),
    }),

  getInvestigation: (investigationId: number) => request<Investigation>(`/investigations/${investigationId}`),

  resumeInvestigation: (investigationId: number, decision: string, reviewerEmployeeId: number, comments?: string) =>
    request<RunInvestigationResult>(`/investigations/${investigationId}/resume`, {
      method: "POST",
      body: JSON.stringify({ decision, reviewerEmployeeId, comments }),
    }),

  getInvestigationRecommendations: (investigationId: number) =>
    request<{ investigation: Investigation; recommendations: InfrastructureRecommendation[] }>(
      `/investigations/${investigationId}/recommendations`,
    ),
};

export { ApiError };
