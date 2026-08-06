using FluentValidation;
using SeekAndDestroy.Application.Dtos;

namespace SeekAndDestroy.Application.Validators;

public sealed class HostingRecommendationRequestValidator : AbstractValidator<HostingRecommendationRequestDto>
{
    public HostingRecommendationRequestValidator()
    {
        RuleFor(x => x.ApplicationCode).NotEmpty().MaximumLength(30).Matches("^[A-Z0-9-]+$");
    }
}

public sealed class CapacityRecommendationRequestValidator : AbstractValidator<CapacityRecommendationRequestDto>
{
    private static readonly string[] Environments = ["Production", "Staging", "Test", "Development"];
    private static readonly string[] Platforms = ["Kubernetes", "VMware", "OpenShift", "BareMetal", "Hyper-V"];
    private static readonly string[] Tiers = ["Tier-1", "Tier-2", "Tier-3"];
    private static readonly string[] Classifications = ["Public", "Internal", "Confidential", "Restricted"];

    public CapacityRecommendationRequestValidator()
    {
        RuleFor(x => x.Environment).Must(Environments.Contains).WithMessage("Environment must be one of Production, Staging, Test, Development.");
        RuleFor(x => x.CpuCores).GreaterThan(0);
        RuleFor(x => x.MemoryGb).GreaterThan(0);
        RuleFor(x => x.StorageGb).GreaterThan(0);
        RuleFor(x => x.Platform).Must(Platforms.Contains).WithMessage("Unsupported platform.");
        RuleFor(x => x.AvailabilityTier).Must(Tiers.Contains).WithMessage("Availability tier must be Tier-1, Tier-2 or Tier-3.");
        RuleFor(x => x.DataClassification).Must(Classifications.Contains).WithMessage("Invalid data classification.");
        RuleFor(x => x.ExpectedGrowthPercent).GreaterThanOrEqualTo(0);
        RuleFor(x => x.RequestedByEmployeeId).GreaterThan(0);
    }
}

public sealed class ForecastRequestValidator : AbstractValidator<ForecastRequestDto>
{
    private static readonly int[] SupportedHorizons = [30, 60, 90, 180];

    public ForecastRequestValidator()
    {
        RuleFor(x => x.ClusterCode).NotEmpty().MaximumLength(30);
        RuleFor(x => x.HorizonDays).Must(SupportedHorizons.Contains).WithMessage("horizonDays must be one of 30, 60, 90, 180.");
    }
}

public sealed class RecommendationDecisionRequestValidator : AbstractValidator<RecommendationDecisionRequestDto>
{
    private static readonly string[] Decisions = ["Approve", "Reject", "RequestMoreAnalysis"];

    public RecommendationDecisionRequestValidator()
    {
        RuleFor(x => x.Decision).Must(Decisions.Contains).WithMessage("Decision must be Approve, Reject or RequestMoreAnalysis.");
        RuleFor(x => x.ReviewerEmployeeId).GreaterThan(0).WithMessage("A reviewer identity is required - decisions cannot be anonymous.");
    }
}

public sealed class ApproveRejectRequestValidator : AbstractValidator<ApproveRejectRequestDto>
{
    public ApproveRejectRequestValidator()
    {
        RuleFor(x => x.ReviewerEmployeeId).GreaterThan(0).WithMessage("A reviewer identity is required - decisions cannot be anonymous.");
    }
}

public sealed class CreateInvestigationRequestValidator : AbstractValidator<CreateInvestigationRequestDto>
{
    public CreateInvestigationRequestValidator()
    {
        RuleFor(x => x.Query).NotEmpty().MaximumLength(2000);
        RuleFor(x => x.CreatedByEmployeeId).GreaterThan(0);
    }
}
