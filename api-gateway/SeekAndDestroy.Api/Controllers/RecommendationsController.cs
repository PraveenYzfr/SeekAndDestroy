using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Api.Controllers;

/// <summary>Every action here is a thin, validated pass-through to the Python
/// AI service - this controller never computes a score, a rule result or a
/// forecast itself. Approve/Reject require a reviewer identity: the request's
/// ReviewerEmployeeId is no longer trusted as-is, it's cross-checked against
/// the caller's authenticated token by the AI service (which forwards the
/// same Bearer token this controller received - see AiServiceClient).</summary>
[ApiController]
[Authorize]
[Route("api/recommendations")]
public sealed class RecommendationsController(IAiServiceClient aiServiceClient) : ControllerBase
{
    [HttpPost("hosting")]
    public async Task<IActionResult> Hosting([FromBody] HostingRecommendationRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.GetHostingRecommendationsAsync(request, ct));

    [HttpPost("capacity")]
    public async Task<IActionResult> Capacity([FromBody] CapacityRecommendationRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.GetCapacityRecommendationsAsync(request, ct));

    [HttpPost("right-sizing")]
    public async Task<IActionResult> RightSizing([FromBody] GatewayRightSizingRequestDto request, CancellationToken ct)
    {
        if (!string.IsNullOrWhiteSpace(request.ApplicationCode))
        {
            return Ok(await aiServiceClient.GetApplicationRightSizingAsync(new RightSizingApplicationRequestDto(request.ApplicationCode), ct));
        }
        return Ok(await aiServiceClient.GetClusterRightSizingAsync(new RightSizingClusterRequestDto(request.ClusterCode), ct));
    }

    [HttpPost("consolidation")]
    public async Task<IActionResult> Consolidation([FromBody] ConsolidationRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.AnalyzeConsolidationAsync(request, ct));

    [HttpPost("forecast")]
    public async Task<IActionResult> Forecast([FromBody] ForecastRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.GetForecastAsync(request, ct));

    [HttpPost("{id:int}/approve")]
    public async Task<IActionResult> Approve(int id, [FromBody] ApproveRejectRequestDto request, CancellationToken ct)
    {
        var decision = new RecommendationDecisionRequestDto("Approve", request.ReviewerEmployeeId, request.Reason);
        return Ok(await aiServiceClient.SubmitRecommendationDecisionAsync(id, decision, ct));
    }

    [HttpPost("{id:int}/reject")]
    public async Task<IActionResult> Reject(int id, [FromBody] ApproveRejectRequestDto request, CancellationToken ct)
    {
        var decision = new RecommendationDecisionRequestDto("Reject", request.ReviewerEmployeeId, request.Reason);
        return Ok(await aiServiceClient.SubmitRecommendationDecisionAsync(id, decision, ct));
    }
}
