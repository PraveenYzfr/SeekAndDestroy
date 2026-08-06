using SeekAndDestroy.Infrastructure.AiService;
using Xunit;

namespace SeekAndDestroy.Tests.Unit;

public class SnakeCaseNamingPolicyTests
{
    [Theory]
    [InlineData("ApplicationCode", "application_code")]
    [InlineData("CpuCores", "cpu_cores")]
    [InlineData("AvailabilityTier", "availability_tier")]
    [InlineData("RequiredByDate", "required_by_date")]
    [InlineData("Id", "id")]
    public void ConvertsPascalCaseToSnakeCase(string input, string expected)
    {
        var actual = SnakeCaseNamingPolicy.Instance.ConvertName(input);
        Assert.Equal(expected, actual);
    }
}
