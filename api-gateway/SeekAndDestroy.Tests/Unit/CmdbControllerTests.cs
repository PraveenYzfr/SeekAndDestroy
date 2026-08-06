using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Moq;
using SeekAndDestroy.Api.Controllers;
using SeekAndDestroy.Application.Interfaces;
using SeekAndDestroy.Domain.Entities;
using Xunit;

namespace SeekAndDestroy.Tests.Unit;

public class CmdbControllerTests
{
    [Fact]
    public async Task GetCluster_ReturnsNotFound_WhenClusterMissing()
    {
        var repo = new Mock<ICmdbRepository>();
        repo.Setup(r => r.GetClusterByIdAsync(999, It.IsAny<CancellationToken>())).ReturnsAsync((InfrastructureCluster?)null);
        var controller = new CmdbController(repo.Object);

        var result = await controller.GetCluster(999, CancellationToken.None);

        var problem = Assert.IsType<ObjectResult>(result);
        Assert.Equal(StatusCodes.Status404NotFound, problem.StatusCode);
    }

    [Fact]
    public async Task GetCluster_ReturnsOk_WhenClusterExists()
    {
        var cluster = new InfrastructureCluster { ClusterId = 3, ClusterCode = "atl-03" };
        var repo = new Mock<ICmdbRepository>();
        repo.Setup(r => r.GetClusterByIdAsync(3, It.IsAny<CancellationToken>())).ReturnsAsync(cluster);
        var controller = new CmdbController(repo.Object);

        var result = await controller.GetCluster(3, CancellationToken.None);

        var ok = Assert.IsType<OkObjectResult>(result);
        var body = Assert.IsType<InfrastructureCluster>(ok.Value);
        Assert.Equal("atl-03", body.ClusterCode);
    }

    [Fact]
    public async Task GetApplications_PassesEnvironmentFilterThrough()
    {
        var repo = new Mock<ICmdbRepository>();
        repo.Setup(r => r.GetApplicationsAsync("Production", It.IsAny<CancellationToken>()))
            .ReturnsAsync(new List<CmdbApplication>());
        var controller = new CmdbController(repo.Object);

        await controller.GetApplications("Production", CancellationToken.None);

        repo.Verify(r => r.GetApplicationsAsync("Production", It.IsAny<CancellationToken>()), Times.Once);
    }
}
