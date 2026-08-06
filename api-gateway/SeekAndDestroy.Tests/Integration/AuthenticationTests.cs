using System.IdentityModel.Tokens.Jwt;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Claims;
using System.Text;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.IdentityModel.Tokens;

namespace SeekAndDestroy.Tests.Integration;

/// <summary>Proves the JWT wiring actually rejects/accepts requests through
/// the real ASP.NET middleware pipeline - CmdbControllerTests etc. instantiate
/// controllers directly and never exercise [Authorize]/UseAuthentication at
/// all, so this is the only coverage that would catch the auth wiring being
/// broken or accidentally left off.</summary>
public sealed class AuthenticationTests(WebApplicationFactory<Program> factory) : IClassFixture<WebApplicationFactory<Program>>
{
    // Matches appsettings.json's default Auth:LocalSigningKey - a real deployment
    // must change both to a real secret; this test intentionally exercises the
    // shipped default so it fails loudly if that default is ever removed.
    private const string SigningKey = "dev-only-insecure-signing-key-change-me";

    private static string MakeToken(int employeeId, DateTime? expires = null)
    {
        var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(SigningKey));
        var creds = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);
        var claims = new[] { new Claim("employee_id", employeeId.ToString()) };
        var token = new JwtSecurityToken(
            claims: claims, expires: expires ?? DateTime.UtcNow.AddMinutes(5), signingCredentials: creds);
        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    [Fact]
    public async Task ProtectedEndpoint_WithoutToken_Returns401()
    {
        var client = factory.CreateClient();
        var response = await client.GetAsync("/api/cmdb/clusters/3");
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task ProtectedEndpoint_WithValidToken_Returns200()
    {
        var client = factory.CreateClient();
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", MakeToken(1));
        var response = await client.GetAsync("/api/cmdb/clusters/3");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public async Task ProtectedEndpoint_WithExpiredToken_Returns401()
    {
        var client = factory.CreateClient();
        client.DefaultRequestHeaders.Authorization =
            new AuthenticationHeaderValue("Bearer", MakeToken(1, expires: DateTime.UtcNow.AddMinutes(-5)));
        var response = await client.GetAsync("/api/cmdb/clusters/3");
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task ProtectedEndpoint_WithTamperedToken_Returns401()
    {
        var client = factory.CreateClient();
        var forged = new JwtSecurityToken(
            claims: new[] { new Claim("employee_id", "1") }, expires: DateTime.UtcNow.AddMinutes(5),
            signingCredentials: new SigningCredentials(
                new SymmetricSecurityKey(Encoding.UTF8.GetBytes("a-completely-different-wrong-key-here")),
                SecurityAlgorithms.HmacSha256));
        var token = new JwtSecurityTokenHandler().WriteToken(forged);
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        var response = await client.GetAsync("/api/cmdb/clusters/3");
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task HealthEndpoint_IsUnauthenticated()
    {
        var client = factory.CreateClient();
        var response = await client.GetAsync("/health");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public async Task MetricsEndpoint_IsUnauthenticatedAndExposesPrometheusFormat()
    {
        var client = factory.CreateClient();
        var response = await client.GetAsync("/metrics");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        var body = await response.Content.ReadAsStringAsync();
        Assert.Contains("# HELP", body);
    }
}
