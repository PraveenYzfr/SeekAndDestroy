using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Api.Controllers;

/// <summary>Model administration: which model serves which role.
///
/// WHY THIS FILE EXISTS
/// --------------------
/// The Model Settings screen was reachable, correctly gated on is_admin, and
/// permanently broken. It calls /api/admin/model-roles; the gateway had
/// Auth, Cmdb, Insights, Investigations and Recommendations controllers and
/// nothing for admin, so every request 404'd and the screen showed only
/// "Request failed".
///
/// The endpoints existed and worked the whole time - a direct call to the AI
/// service returned both payloads with real data. Nothing was wrong except
/// that no route carried them the last hop, which is why it looked like a
/// broken screen rather than a missing one.
///
/// AUTHORIZATION IS NOT DUPLICATED HERE
/// ------------------------------------
/// [Authorize] establishes that the caller is authenticated; whether they are
/// an ADMINISTRATOR is decided by the AI service, which re-reads IsAdmin from
/// the database on every request rather than trusting a token claim. That is
/// the platform's stated trust boundary and it is what makes revoking someone
/// take effect immediately instead of at token expiry.
///
/// Re-deciding it here from a claim would be a second answer to the same
/// question, computed from staler data, and the two would diverge silently the
/// first time an administrator was demoted mid-session. A non-admin gets a 403
/// forwarded from the AI service - which is what the UI already expects: "403
/// rather than a hidden route when the caller is not an administrator - the API
/// decides, the screen only reports what it was told."</summary>
[ApiController]
[Authorize]
[Route("api/admin")]
public sealed class AdminController(IAiServiceClient aiServiceClient) : ControllerBase
{
    [HttpGet("model-roles")]
    public async Task<IActionResult> GetModelRoles(CancellationToken ct) =>
        Ok(await aiServiceClient.GetModelRolesAsync(ct));

    /// <summary><paramref name="refresh"/> re-asks every provider instead of
    /// serving the AI service's ten-minute cache. Worth forwarding rather than
    /// swallowing: a model retired minutes ago is precisely the case where the
    /// cached list is confidently wrong.</summary>
    [HttpGet("model-providers")]
    public async Task<IActionResult> GetModelProviders([FromQuery] bool refresh, CancellationToken ct) =>
        Ok(await aiServiceClient.GetModelProvidersAsync(refresh, ct));

    /// <summary>The evaluation scorecard. Graded from sad.AgentAuditLog, so it
    /// calls no model and costs a table scan - the expensive part already
    /// happened and was already paid for.
    ///
    /// Exposed because the Model Settings screen told an administrator to run
    /// scripts/evaluate.py, and scripts/ is not in the service image. Nobody
    /// could follow that instruction on the deployed system.</summary>
    [HttpGet("evaluation")]
    public async Task<IActionResult> GetEvaluation([FromQuery] int limit, CancellationToken ct) =>
        Ok(await aiServiceClient.GetEvaluationAsync(limit <= 0 ? 5000 : limit, ct));

    /// <summary>The full model exchange for one investigation - prompt, output,
    /// model and latency - with each grader's stored verdict beside it.
    ///
    /// Added at the same time as the AI service endpoint rather than after,
    /// because a route that exists on the service and not on the gateway is
    /// invisible to every caller that goes through the site. That is exactly how
    /// Model Settings shipped able to 404 for its whole life.</summary>
    [HttpGet("investigations/{investigationId:int}/transcript")]
    public async Task<IActionResult> GetTranscript(int investigationId, CancellationToken ct) =>
        Ok(await aiServiceClient.GetInvestigationTranscriptAsync(investigationId, ct));

    /// <summary>Conversations to inspect, worst first - the reason to open the
    /// list is to find a bad answer, not the newest one.</summary>
    [HttpGet("conversations")]
    public async Task<IActionResult> ListConversations([FromQuery] int limit, CancellationToken ct) =>
        Ok(await aiServiceClient.ListConversationsAsync(limit <= 0 ? 50 : limit, ct));

    /// <summary>One conversation at all three levels: the session score, each
    /// turn's score, and the calls behind a turn. The three are computed from the
    /// underlying counts rather than from each other - averaging turn rates would
    /// let a one-line reply weigh as much as a full report.</summary>
    [HttpGet("conversations/{conversationId}")]
    public async Task<IActionResult> GetConversationDetail(string conversationId, CancellationToken ct) =>
        Ok(await aiServiceClient.GetConversationDetailAsync(conversationId, ct));

    [HttpGet("conversations/{conversationId}/evaluation")]
    public async Task<IActionResult> GetConversationEvaluation(string conversationId, CancellationToken ct) =>
        Ok(await aiServiceClient.GetConversationEvaluationAsync(conversationId, ct));

    [HttpPut("model-roles/{roleName}")]
    public async Task<IActionResult> SetModelRole(string roleName, [FromBody] ModelRoleAssignmentDto request, CancellationToken ct) =>
        Ok(await aiServiceClient.SetModelRoleAsync(roleName, request, ct));

    /// <summary>Removes the override so the role falls back to the configured
    /// default. Not a deletion of anything an engineer authored - the response
    /// reports whether a row was actually removed.</summary>
    [HttpDelete("model-roles/{roleName}")]
    public async Task<IActionResult> ClearModelRole(string roleName, CancellationToken ct) =>
        Ok(await aiServiceClient.ClearModelRoleAsync(roleName, ct));
}
