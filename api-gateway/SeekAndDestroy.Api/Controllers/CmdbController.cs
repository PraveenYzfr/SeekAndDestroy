using Microsoft.AspNetCore.Mvc;
using SeekAndDestroy.Application.Interfaces;

namespace SeekAndDestroy.Api.Controllers;

[ApiController]
[Route("api/cmdb")]
public sealed class CmdbController(ICmdbRepository repository) : ControllerBase
{
    [HttpGet("applications")]
    public async Task<IActionResult> GetApplications([FromQuery] string? environment, CancellationToken ct)
    {
        var applications = await repository.GetApplicationsAsync(environment, ct);
        return Ok(applications);
    }

    [HttpGet("clusters")]
    public async Task<IActionResult> GetClusters([FromQuery] string? environment, CancellationToken ct)
    {
        var clusters = await repository.GetClustersAsync(environment, ct);
        return Ok(clusters);
    }

    [HttpGet("clusters/{id:int}")]
    public async Task<IActionResult> GetCluster(int id, CancellationToken ct)
    {
        var cluster = await repository.GetClusterByIdAsync(id, ct);
        if (cluster is null)
        {
            return Problem(title: "Cluster not found", detail: $"No cluster with id {id}.", statusCode: StatusCodes.Status404NotFound);
        }
        return Ok(cluster);
    }
}
