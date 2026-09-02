using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Api.Controllers;

/// <summary>CMDB Insighter: a single free-text-question endpoint. The AI
/// service (app.api.routes_insights / app.insights.router) does all the
/// classification and computation; this controller only authenticates,
/// forwards, and passes the response straight through - same shape as
/// InvestigationsController.</summary>
[ApiController]
[Authorize]
[Route("api/insights")]
public sealed class InsightsController(IAiServiceClient aiServiceClient) : ControllerBase
{
    [HttpPost("ask")]
    public async Task<IActionResult> Ask([FromBody] InsightAskRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.AskInsightAsync(request, ct));
}
