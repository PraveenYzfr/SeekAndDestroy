using SeekAndDestroy.Application.Dtos;
using SeekAndDestroy.Application.Validators;
using Xunit;

namespace SeekAndDestroy.Tests.Unit;

public class ValidatorTests
{
    [Fact]
    public void CapacityRequest_RejectsZeroCpu()
    {
        var validator = new CapacityRecommendationRequestValidator();
        var request = new CapacityRecommendationRequestDto(
            "Production", 0m, 32m, 500m, "Kubernetes", "Tier-2", "Internal", null, 10m, null, 1, null);

        var result = validator.Validate(request);

        Assert.False(result.IsValid);
        Assert.Contains(result.Errors, e => e.PropertyName == nameof(CapacityRecommendationRequestDto.CpuCores));
    }

    [Fact]
    public void CapacityRequest_RejectsInvalidPlatform()
    {
        var validator = new CapacityRecommendationRequestValidator();
        var request = new CapacityRecommendationRequestDto(
            "Production", 8m, 32m, 500m, "OpenVMS", "Tier-2", "Internal", null, 10m, null, 1, null);

        var result = validator.Validate(request);

        Assert.False(result.IsValid);
        Assert.Contains(result.Errors, e => e.PropertyName == nameof(CapacityRecommendationRequestDto.Platform));
    }

    [Fact]
    public void CapacityRequest_AcceptsWellFormedRequest()
    {
        var validator = new CapacityRecommendationRequestValidator();
        var request = new CapacityRecommendationRequestDto(
            "Production", 8m, 32m, 500m, "Kubernetes", "Tier-2", "Internal", "Atlanta-DC1", 10m, null, 1, null);

        var result = validator.Validate(request);

        Assert.True(result.IsValid);
    }

    [Theory]
    [InlineData(30)]
    [InlineData(60)]
    [InlineData(90)]
    [InlineData(180)]
    public void ForecastRequest_AcceptsSupportedHorizons(int horizon)
    {
        var validator = new ForecastRequestValidator();
        var result = validator.Validate(new ForecastRequestDto("atl-03", horizon));
        Assert.True(result.IsValid);
    }

    [Fact]
    public void ForecastRequest_RejectsUnsupportedHorizon()
    {
        var validator = new ForecastRequestValidator();
        var result = validator.Validate(new ForecastRequestDto("atl-03", 45));
        Assert.False(result.IsValid);
    }

    [Fact]
    public void ApproveReject_RequiresPositiveReviewerId()
    {
        var validator = new ApproveRejectRequestValidator();
        var result = validator.Validate(new ApproveRejectRequestDto(0, "reason"));
        Assert.False(result.IsValid);
    }

    [Theory]
    [InlineData("Approve")]
    [InlineData("Reject")]
    [InlineData("RequestMoreAnalysis")]
    public void RecommendationDecision_AcceptsKnownDecisions(string decision)
    {
        var validator = new RecommendationDecisionRequestValidator();
        var result = validator.Validate(new RecommendationDecisionRequestDto(decision, 1, null));
        Assert.True(result.IsValid);
    }

    [Fact]
    public void RecommendationDecision_RejectsUnknownDecision()
    {
        var validator = new RecommendationDecisionRequestValidator();
        var result = validator.Validate(new RecommendationDecisionRequestDto("Execute", 1, null));
        Assert.False(result.IsValid);
    }
}
