using System.Text;
using System.Text.Json;

namespace SeekAndDestroy.Infrastructure.AiService;

/// <summary>The Python AI service's Pydantic models use snake_case field
/// names (application_code, cpu_cores, ...) with no alias generator, so
/// requests built from C# PascalCase records must be re-cased on the wire.
/// net8.0 has no built-in snake_case policy (that arrived in net9.0), hence
/// this small implementation.</summary>
public sealed class SnakeCaseNamingPolicy : JsonNamingPolicy
{
    public static readonly SnakeCaseNamingPolicy Instance = new();

    public override string ConvertName(string name)
    {
        var builder = new StringBuilder(name.Length + 8);
        for (var i = 0; i < name.Length; i++)
        {
            var c = name[i];
            if (char.IsUpper(c))
            {
                if (i > 0) builder.Append('_');
                builder.Append(char.ToLowerInvariant(c));
            }
            else
            {
                builder.Append(c);
            }
        }
        return builder.ToString();
    }
}
