using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Api.Controllers;

[ApiController]
[Authorize]
[Route("api/investigations")]
public sealed class InvestigationsController(IAiServiceClient aiServiceClient) : ControllerBase
{
    [HttpPost]
    public async Task<IActionResult> Create([FromBody] CreateInvestigationRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.CreateInvestigationAsync(request, ct));

    [HttpGet("{id:int}")]
    public async Task<IActionResult> Get(int id, CancellationToken ct) =>
        Ok(await aiServiceClient.GetInvestigationAsync(id, ct));

    [HttpPost("{id:int}/resume")]
    public async Task<IActionResult> Resume(int id, [FromBody] ResumeInvestigationRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.ResumeInvestigationAsync(id, request, ct));

    [HttpGet("{id:int}/recommendations")]
    public async Task<IActionResult> GetRecommendations(int id, CancellationToken ct) =>
        Ok(await aiServiceClient.GetInvestigationRecommendationsAsync(id, ct));

    /// <summary>What the person who read this answer thought of it.
    ///
    /// NOT admin-gated, unlike the model and evaluation routes. The person who
    /// read the report is the one qualified to say whether it helped, and that
    /// is rarely an administrator - gating it would leave the only human signal
    /// in the platform reachable by the handful of people least likely to be
    /// reading reports.
    ///
    /// The employee id is never taken from the request. It comes from the
    /// bearer token in the AI service, so nobody can rate as somebody else.</summary>
    [HttpPost("{id:int}/feedback")]
    public async Task<IActionResult> SubmitFeedback(int id, [FromBody] AnswerFeedbackRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.SubmitAnswerFeedbackAsync(id, request, ct));

    [HttpGet("{id:int}/feedback")]
    public async Task<IActionResult> GetMyFeedback(int id, CancellationToken ct) =>
        Ok(await aiServiceClient.GetMyAnswerFeedbackAsync(id, ct));
}
