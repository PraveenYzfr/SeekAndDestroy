using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Exceptions;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Infrastructure.AiService;

/// <summary>Typed HttpClient over the FastAPI AI service. Registered via
/// AddHttpClient in Program.cs with BaseAddress from configuration
/// (AiService:BaseUrl) - never hardcoded here.</summary>
public sealed class AiServiceClient(HttpClient httpClient, IHttpContextAccessor httpContextAccessor, ILogger<AiServiceClient> logger) : IAiServiceClient
{
    private static readonly JsonSerializerOptions RequestOptions = new()
    {
        PropertyNamingPolicy = SnakeCaseNamingPolicy.Instance,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>Forwards the caller's own Bearer token through to the AI
    /// service unchanged, so it validates the exact same token the gateway
    /// did - real defense in depth (nothing that skips the gateway can reach
    /// the AI service unauthenticated either), not the gateway re-minting or
    /// vouching for an identity on the caller's behalf. Absent for the one
    /// unauthenticated call the gateway itself makes - issuing a dev token.</summary>
    private AuthenticationHeaderValue? CallerAuthorizationHeader()
    {
        var header = httpContextAccessor.HttpContext?.Request.Headers["Authorization"].FirstOrDefault();
        if (string.IsNullOrWhiteSpace(header) || !AuthenticationHeaderValue.TryParse(header, out var parsed))
        {
            return null;
        }
        return parsed;
    }

    private async Task<JsonNode?> PostAsync(string path, object body, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, path)
        {
            Content = JsonContent.Create(body, options: RequestOptions),
        };
        request.Headers.Authorization = CallerAuthorizationHeader();
        using var response = await httpClient.SendAsync(request, ct);
        var text = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
        {
            logger.LogWarning("AI service {Path} returned {Status}: {Body}", path, (int)response.StatusCode, text);
            throw new AiServiceException((int)response.StatusCode, text);
        }
        return string.IsNullOrWhiteSpace(text) ? null : JsonNode.Parse(text);
    }

    private async Task<JsonNode?> GetAsync(string path, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, path);
        request.Headers.Authorization = CallerAuthorizationHeader();
        using var response = await httpClient.SendAsync(request, ct);
        var text = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
        {
            logger.LogWarning("AI service {Path} returned {Status}: {Body}", path, (int)response.StatusCode, text);
            throw new AiServiceException((int)response.StatusCode, text);
        }
        return string.IsNullOrWhiteSpace(text) ? null : JsonNode.Parse(text);
    }

    /// <summary>PUT and DELETE exist only for the model-role overrides. Both
    /// carry the caller's own token and surface the AI service's status
    /// verbatim, exactly as PostAsync and GetAsync do - a 403 for a non-admin
    /// must reach the screen as a 403, not as a generic gateway failure.</summary>
    private async Task<JsonNode?> SendAsync(HttpMethod method, string path, object? body, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(method, path);
        if (body is not null)
        {
            request.Content = JsonContent.Create(body, options: RequestOptions);
        }
        request.Headers.Authorization = CallerAuthorizationHeader();
        using var response = await httpClient.SendAsync(request, ct);
        var text = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
        {
            logger.LogWarning("AI service {Path} returned {Status}: {Body}", path, (int)response.StatusCode, text);
            throw new AiServiceException((int)response.StatusCode, text);
        }
        return string.IsNullOrWhiteSpace(text) ? null : JsonNode.Parse(text);
    }

    public Task<JsonNode?> GetHostingRecommendationsAsync(HostingRecommendationRequestDto request, CancellationToken ct) =>
        PostAsync("/api/hosting/recommendations", request, ct);

    public Task<JsonNode?> GetCapacityRecommendationsAsync(CapacityRecommendationRequestDto request, CancellationToken ct) =>
        PostAsync("/api/capacity/recommendations", request, ct);

    public Task<JsonNode?> GetClusterRightSizingAsync(RightSizingClusterRequestDto request, CancellationToken ct) =>
        PostAsync("/api/right-sizing/clusters", request, ct);

    public Task<JsonNode?> GetApplicationRightSizingAsync(RightSizingApplicationRequestDto request, CancellationToken ct) =>
        PostAsync("/api/right-sizing/applications", request, ct);

    public Task<JsonNode?> AnalyzeConsolidationAsync(ConsolidationRequestDto request, CancellationToken ct) =>
        PostAsync("/api/consolidation/analyze", request, ct);

    public Task<JsonNode?> GetForecastAsync(ForecastRequestDto request, CancellationToken ct) =>
        PostAsync("/api/forecast", request, ct);

    public Task<JsonNode?> AskInsightAsync(InsightAskRequestDto request, CancellationToken ct) =>
        PostAsync("/api/insights/ask", request, ct);

    public Task<JsonNode?> GetModelRolesAsync(CancellationToken ct) =>
        GetAsync("/api/admin/model-roles", ct);

    public Task<JsonNode?> GetModelProvidersAsync(bool refresh, CancellationToken ct) =>
        GetAsync(refresh ? "/api/admin/model-providers?refresh=true" : "/api/admin/model-providers", ct);

    public Task<JsonNode?> SetModelRoleAsync(string roleName, ModelRoleAssignmentDto request, CancellationToken ct) =>
        SendAsync(HttpMethod.Put, $"/api/admin/model-roles/{Uri.EscapeDataString(roleName)}", request, ct);

    public Task<JsonNode?> ClearModelRoleAsync(string roleName, CancellationToken ct) =>
        SendAsync(HttpMethod.Delete, $"/api/admin/model-roles/{Uri.EscapeDataString(roleName)}", null, ct);

    public Task<JsonNode?> GetEvaluationAsync(int limit, CancellationToken ct) =>
        GetAsync($"/api/admin/evaluation?limit={limit}", ct);

    public Task<JsonNode?> GetInvestigationTranscriptAsync(int investigationId, CancellationToken ct) =>
        GetAsync($"/api/admin/investigations/{investigationId}/transcript", ct);

    public Task<JsonNode?> GetConversationEvaluationAsync(string conversationId, CancellationToken ct) =>
        GetAsync($"/api/admin/conversations/{Uri.EscapeDataString(conversationId)}/evaluation", ct);

    public Task<JsonNode?> GetConversationDetailAsync(string conversationId, CancellationToken ct) =>
        GetAsync($"/api/admin/conversations/{Uri.EscapeDataString(conversationId)}", ct);

    public Task<JsonNode?> CreateInvestigationAsync(CreateInvestigationRequestDto request, CancellationToken ct) =>
        PostAsync("/api/investigations", request, ct);

    public Task<JsonNode?> GetInvestigationAsync(int investigationId, CancellationToken ct) =>
        GetAsync($"/api/investigations/{investigationId}", ct);

    public Task<JsonNode?> ResumeInvestigationAsync(int investigationId, ResumeInvestigationRequestDto request, CancellationToken ct) =>
        PostAsync($"/api/investigations/{investigationId}/resume", request, ct);

    public Task<JsonNode?> GetInvestigationRecommendationsAsync(int investigationId, CancellationToken ct) =>
        GetAsync($"/api/investigations/{investigationId}/recommendations", ct);

    public Task<JsonNode?> SubmitRecommendationDecisionAsync(int recommendationId, RecommendationDecisionRequestDto request, CancellationToken ct) =>
        PostAsync($"/api/recommendations/{recommendationId}/decision", request, ct);

    public Task<JsonNode?> IssueDevTokenAsync(DevTokenRequestDto request, CancellationToken ct) =>
        PostUnauthenticatedAsync("/api/auth/dev-token", request, ct);

    public Task<JsonNode?> LoginAsync(LoginRequestDto request, CancellationToken ct) =>
        PostUnauthenticatedAsync("/api/auth/login", request, ct);

    /// <summary>The two token-issuing calls. Deliberately does not go through
    /// PostAsync/CallerAuthorizationHeader - these are how a token is obtained
    /// in the first place, so there is never a caller Authorization header
    /// worth forwarding. The failure log records the status only, never the
    /// response body or the request: a login body carries a password.</summary>
    private async Task<JsonNode?> PostUnauthenticatedAsync(string path, object request, CancellationToken ct)
    {
        using var response = await httpClient.PostAsJsonAsync(path, request, RequestOptions, ct);
        var text = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
        {
            logger.LogWarning("AI service {Path} returned {Status}", path, (int)response.StatusCode);
            throw new AiServiceException((int)response.StatusCode, text);
        }
        return string.IsNullOrWhiteSpace(text) ? null : JsonNode.Parse(text);
    }
}
