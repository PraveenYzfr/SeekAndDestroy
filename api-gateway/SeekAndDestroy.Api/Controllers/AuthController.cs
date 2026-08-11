using Microsoft.AspNetCore.Mvc;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Api.Controllers;

/// <summary>Deliberately unauthenticated - proxies to the AI service's own
/// dev-token issuance (SAD_AUTH__MODE=local only, 404s in oidc mode), which
/// requires the caller to already exist as a real, active Employee row. This
/// is how a token is obtained in the first place; every other controller in
/// the gateway requires one.</summary>
[ApiController]
[Route("api/auth")]
public sealed class AuthController(IAiServiceClient aiServiceClient) : ControllerBase
{
    [HttpPost("dev-token")]
    public async Task<IActionResult> DevToken([FromBody] DevTokenRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.IssueDevTokenAsync(request, ct));

    /// <summary>Username/password sign-in - the flow the UI uses. Validation
    /// of the credential happens in the AI service against sad.Employee; this
    /// controller only forwards, and never logs the body.</summary>
    [HttpPost("login")]
    public async Task<IActionResult> Login([FromBody] LoginRequestDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.LoginAsync(request, ct));
}
