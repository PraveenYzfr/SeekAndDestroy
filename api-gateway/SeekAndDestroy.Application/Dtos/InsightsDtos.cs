namespace SeekAndDestroy.Application.Dtos;

/// <summary>One free-text question for the CMDB Insighter. No employee id
/// here, unlike CreateInvestigationRequestDto - this feature does not (yet)
/// thread a conversation or record who asked, so there is nothing for the
/// gateway to cross-check against the caller's token. The AI service still
/// authenticates and rate-limits the call itself.</summary>
public sealed record InsightAskRequestDto(string Query);
