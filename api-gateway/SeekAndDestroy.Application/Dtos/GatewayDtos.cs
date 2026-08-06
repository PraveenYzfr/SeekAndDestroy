namespace SeekAndDestroy.Application.Dtos;

/// <summary>Gateway-facing right-sizing request: the specification calls for
/// one POST /api/recommendations/right-sizing endpoint, while the AI service
/// exposes separate cluster/application analyses - this DTO lets the caller
/// pick a scope and the controller routes to the matching AI service call.</summary>
public sealed record GatewayRightSizingRequestDto(string? ClusterCode, string? ApplicationCode);

public sealed record ApproveRejectRequestDto(int ReviewerEmployeeId, string? Reason);

public sealed record DevTokenRequestDto(string EmployeeNumber);
