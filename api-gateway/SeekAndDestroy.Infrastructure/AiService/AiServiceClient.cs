using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Exceptions;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Infrastructure.AiService;

/// <summary>Typed HttpClient over the FastAPI AI service. Registered via
/// AddHttpClient in Program.cs with BaseAddress from configuration
/// (AiService:BaseUrl) - never hardcoded here.</summary>
public sealed class AiServiceClient(HttpClient httpClient, ILogger<AiServiceClient> logger) : IAiServiceClient
{
    private static readonly JsonSerializerOptions RequestOptions = new()
    {
        PropertyNamingPolicy = SnakeCaseNamingPolicy.Instance,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    private async Task<JsonNode?> PostAsync(string path, object body, CancellationToken ct)
    {
        using var response = await httpClient.PostAsJsonAsync(path, body, RequestOptions, ct);
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
        using var response = await httpClient.GetAsync(path, ct);
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
}
