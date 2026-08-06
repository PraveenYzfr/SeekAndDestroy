using Microsoft.Data.SqlClient;
using Microsoft.Extensions.Configuration;

namespace SeekAndDestroy.Infrastructure.Persistence;

/// <summary>Single source of truth for the SQL Server connection on the .NET
/// side, mirroring app.config.settings.DatabaseSettings on the Python side -
/// the connection string lives only in appsettings.json, never duplicated
/// inline in a repository class.</summary>
public interface ISqlConnectionFactory
{
    SqlConnection Create();
}

public sealed class SqlConnectionFactory : ISqlConnectionFactory
{
    private readonly string _connectionString;

    public SqlConnectionFactory(IConfiguration configuration)
    {
        _connectionString = configuration.GetConnectionString("PraveenDB")
            ?? throw new InvalidOperationException("ConnectionStrings:PraveenDB is not configured.");
    }

    public SqlConnection Create() => new(_connectionString);
}
