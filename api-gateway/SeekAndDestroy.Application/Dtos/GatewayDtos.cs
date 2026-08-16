namespace SeekAndDestroy.Application.Dtos;

/// <summary>Gateway-facing right-sizing request: the specification calls for
/// one POST /api/recommendations/right-sizing endpoint, while the AI service
/// exposes separate cluster/application analyses - this DTO lets the caller
/// pick a scope and the controller routes to the matching AI service call.</summary>
public sealed record GatewayRightSizingRequestDto(string? ClusterCode, string? ApplicationCode, bool Explain = false);

public sealed record ApproveRejectRequestDto(int ReviewerEmployeeId, string? Reason);

public sealed record DevTokenRequestDto(string EmployeeNumber);

/// <summary>Username/password sign-in. Username is the employee number or the
/// email address. Never logged, never echoed - see AiServiceClient.</summary>
public sealed record LoginRequestDto(string Username, string Password);
