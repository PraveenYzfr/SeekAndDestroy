using System.Text.Json.Nodes;
using SeekAndDestroy.Application.Dtos;

namespace SeekAndDestroy.Application.Interfaces;

/// <summary>Typed client over the Python AI service. Every method is a thin,
/// auditable pass-through to one FastAPI endpoint - the gateway never
/// recomputes or second-guesses a score, a rule result or a forecast; it only
/// maps the AI service's JSON into gateway DTOs and enforces auth/authorization.</summary>
public interface IAiServiceClient
{
    Task<JsonNode?> GetHostingRecommendationsAsync(HostingRecommendationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetCapacityRecommendationsAsync(CapacityRecommendationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetClusterRightSizingAsync(RightSizingClusterRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetApplicationRightSizingAsync(RightSizingApplicationRequestDto request, CancellationToken ct);
    Task<JsonNode?> AnalyzeConsolidationAsync(ConsolidationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetForecastAsync(ForecastRequestDto request, CancellationToken ct);

    /// <summary>CMDB Insighter: one free-text question, one composed answer.
    /// See app.insights.router (AI service) for how the question is
    /// classified and answered - the gateway passes the payload through
    /// unmodified in both directions.</summary>
    Task<JsonNode?> AskInsightAsync(InsightAskRequestDto request, CancellationToken ct);

    /// <summary>Model administration - which model serves which role, and
    /// what each provider currently offers. Pass-through in both directions:
    /// the AI service owns the role list and decides whether the caller is an
    /// administrator, by re-reading IsAdmin from the database.</summary>
    Task<JsonNode?> GetModelRolesAsync(CancellationToken ct);
    Task<JsonNode?> GetModelProvidersAsync(bool refresh, CancellationToken ct);
    Task<JsonNode?> SetModelRoleAsync(string roleName, ModelRoleAssignmentDto request, CancellationToken ct);
    Task<JsonNode?> ClearModelRoleAsync(string roleName, CancellationToken ct);

    /// <summary>The evaluation scorecard, graded from recorded calls. Calls no
    /// model and spends nothing.</summary>
    Task<JsonNode?> GetEvaluationAsync(int limit, CancellationToken ct);

    /// <summary>Every model call in one investigation with its stored grade,
    /// and the per-grader rollup for a whole conversation. Reads recorded
    /// verdicts; grades nothing on the fly.</summary>
    Task<JsonNode?> GetInvestigationTranscriptAsync(int investigationId, CancellationToken ct);
    Task<JsonNode?> GetConversationDetailAsync(string conversationId, CancellationToken ct);

    /// <summary>Graph failures that used to be dropped, with how often each
    /// drop site fires. Read-only - nothing acts on them yet.</summary>
    Task<JsonNode?> GetRemediationQueueAsync(string? status, int limit, CancellationToken ct);
    Task<JsonNode?> ListConversationsAsync(int limit, CancellationToken ct);
    Task<JsonNode?> GetConversationEvaluationAsync(string conversationId, CancellationToken ct);

    Task<JsonNode?> CreateInvestigationAsync(CreateInvestigationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetInvestigationAsync(int investigationId, CancellationToken ct);
    Task<JsonNode?> ResumeInvestigationAsync(int investigationId, ResumeInvestigationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetInvestigationRecommendationsAsync(int investigationId, CancellationToken ct);

    /// <summary>THE ONLY HUMAN GROUND TRUTH IN THIS PLATFORM. Fidelity is
    /// arithmetic, completeness is field presence, and the judge is one model's
    /// opinion of another's work - none of them has ever been checked against a
    /// person. Without these two calls the table, the repository and the
    /// endpoints all existed and NOBODY COULD REACH THEM.</summary>
    Task<JsonNode?> SubmitAnswerFeedbackAsync(int investigationId, AnswerFeedbackRequestDto request, CancellationToken ct);

    /// <summary>This caller's own rating, so the control renders in the state
    /// they left rather than resetting and inviting a second, contradictory
    /// vote.</summary>
    Task<JsonNode?> GetMyAnswerFeedbackAsync(int investigationId, CancellationToken ct);

    /// <summary>The reasons a rating may carry. Served from the AI service so
    /// the UI keeps no copy of a list that must match the server's.</summary>
    Task<JsonNode?> GetFeedbackReasonsAsync(CancellationToken ct);

    Task<JsonNode?> SubmitRecommendationDecisionAsync(int recommendationId, RecommendationDecisionRequestDto request, CancellationToken ct);

    /// <summary>Proxies to the AI service's own dev-token issuance
    /// (SAD_AUTH__MODE=local only - 404s otherwise). This is the one call
    /// that must NOT forward the caller's Authorization header, since there
    /// isn't one yet - it's how a token is obtained in the first place.</summary>
    Task<JsonNode?> IssueDevTokenAsync(DevTokenRequestDto request, CancellationToken ct);

    /// <summary>Proxies username/password sign-in to the AI service
    /// (SAD_AUTH__MODE=local only - 404s otherwise). Like dev-token, this must
    /// NOT forward a caller Authorization header: it is how a token is
    /// obtained in the first place.</summary>
    Task<JsonNode?> LoginAsync(LoginRequestDto request, CancellationToken ct);
}
