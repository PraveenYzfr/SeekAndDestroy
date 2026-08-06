using Microsoft.AspNetCore.Mvc;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Api.Controllers;

[ApiController]
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
}
