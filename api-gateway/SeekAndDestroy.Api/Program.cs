using System.Text;
using FluentValidation;
using FluentValidation.AspNetCore;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Diagnostics;
using Microsoft.IdentityModel.Tokens;
using Prometheus;
using Serilog;
using SeekAndDestroy.Application.Exceptions;
using SeekAndDestroy.Application.Interfaces;
using SeekAndDestroy.Application.Validators;
using SeekAndDestroy.Infrastructure.AiService;
using SeekAndDestroy.Infrastructure.Persistence;

var builder = WebApplication.CreateBuilder(args);

builder.Host.UseSerilog((context, services, configuration) => configuration
    .ReadFrom.Configuration(context.Configuration)
    .Enrich.FromLogContext()
    .Enrich.WithProperty("Service", "SeekAndDestroy.Api"));

builder.Services.AddControllers();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen(options =>
{
    options.SwaggerDoc("v1", new Microsoft.OpenApi.Models.OpenApiInfo
    {
        Title = "SeekAndDestroy API Gateway",
        Version = "v1",
        Description = "ASP.NET Core gateway in front of the SeekAndDestroy AI service and CMDB.",
    });
});

builder.Services.AddFluentValidationAutoValidation();
builder.Services.AddValidatorsFromAssemblyContaining<CapacityRecommendationRequestValidator>();

builder.Services.AddScoped<ISqlConnectionFactory, SqlConnectionFactory>();
builder.Services.AddScoped<ICmdbRepository, CmdbRepository>();

// Needed so AiServiceClient can forward the caller's own Bearer token
// straight through to the AI service (see comment there) instead of the
// gateway re-minting or stripping it.
builder.Services.AddHttpContextAccessor();

builder.Services.AddHttpClient<IAiServiceClient, AiServiceClient>(client =>
{
    var baseUrl = builder.Configuration["AiService:BaseUrl"] ?? "http://127.0.0.1:8088";
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout = TimeSpan.FromSeconds(builder.Configuration.GetValue("AiService:TimeoutSeconds", 60));
});

// JWT auth: "local" validates tokens this same platform issued itself (via
// POST /api/auth/dev-token, proxied to the AI service - see AuthController)
// using a symmetric key that must match SAD_AUTH__LOCAL_SIGNING_KEY on the AI
// service exactly. "oidc" validates tokens from a real external identity
// provider (Azure AD/Entra, Okta, ...) via standard JWKS - mirrors
// app.security.jwt_service on the Python side so both layers trust the exact
// same tokens.
var authMode = builder.Configuration["Auth:Mode"] ?? "local";
builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme).AddJwtBearer(options =>
{
    if (authMode == "oidc")
    {
        options.Authority = builder.Configuration["Auth:OidcAuthority"];
        options.Audience = builder.Configuration["Auth:OidcAudience"];
        options.RequireHttpsMetadata = true;
    }
    else
    {
        var signingKey = builder.Configuration["Auth:LocalSigningKey"]
            ?? throw new InvalidOperationException("Auth:LocalSigningKey missing for Auth:Mode=local");
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(signingKey)),
            ValidateIssuer = false,
            ValidateAudience = false,
            ValidateLifetime = true,
        };
    }
});
builder.Services.AddAuthorization();

builder.Services.AddHealthChecks()
    .AddSqlServer(
        builder.Configuration.GetConnectionString("PraveenDB") ?? throw new InvalidOperationException("ConnectionStrings:PraveenDB missing"),
        name: "sql-server");

builder.Services.AddCors(options => options.AddDefaultPolicy(policy =>
    policy.AllowAnyOrigin().AllowAnyMethod().AllowAnyHeader()));

var app = builder.Build();

app.UseSerilogRequestLogging();

if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

// Forwards the AI service's own RFC 7807 ProblemDetails body/status verbatim
// instead of masking every non-2xx AI-service response behind a generic 500.
app.UseExceptionHandler(errorApp => errorApp.Run(async context =>
{
    var feature = context.Features.Get<IExceptionHandlerFeature>();
    if (feature?.Error is AiServiceException aiEx)
    {
        context.Response.StatusCode = aiEx.StatusCode is >= 100 and < 600 ? aiEx.StatusCode : StatusCodes.Status502BadGateway;
        context.Response.ContentType = "application/problem+json";
        if (!string.IsNullOrWhiteSpace(aiEx.ResponseBody))
        {
            await context.Response.WriteAsync(aiEx.ResponseBody);
        }
        return;
    }

    context.Response.StatusCode = StatusCodes.Status500InternalServerError;
    context.Response.ContentType = "application/problem+json";
    await context.Response.WriteAsJsonAsync(new
    {
        type = "about:blank",
        title = "Internal server error",
        status = 500,
        detail = "An unexpected error occurred.",
        instance = context.Request.Path.Value,
    });
}));

app.UseCors();
app.UseAuthentication();
app.UseAuthorization();

// Standard HTTP request-rate/latency/status-code metrics at GET /metrics -
// unauthenticated, matching /health (a Prometheus scraper is another
// standard infra probe, not a platform client).
app.UseHttpMetrics();

app.MapControllers();
app.MapHealthChecks("/health");
app.MapMetrics();

app.Run();

public partial class Program { } // exposed for WebApplicationFactory in tests
