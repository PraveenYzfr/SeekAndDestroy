namespace SeekAndDestroy.Application.Exceptions;

/// <summary>Thrown when the AI service returns a non-success status. The
/// gateway forwards the AI service's RFC 7807 ProblemDetails body and status
/// code verbatim (see Program.cs's exception handler) instead of masking it
/// behind a generic 500.</summary>
public sealed class AiServiceException(int statusCode, string responseBody) : Exception($"AI service returned {statusCode}: {responseBody}")
{
    public int StatusCode { get; } = statusCode;
    public string ResponseBody { get; } = responseBody;
}
