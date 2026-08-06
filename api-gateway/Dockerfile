FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY . .
# Restore the project directly, not SeekAndDestroy.slnx - the .slnx (XML
# solution) format isn't understood by the .NET 8 SDK's dotnet restore/msbuild
# (it needs a newer SDK); the app itself still targets net8.0 and runs fine
# on the aspnet:8.0 runtime image below, so only this restore target changes.
RUN dotnet restore SeekAndDestroy.Api/SeekAndDestroy.Api.csproj
RUN dotnet publish SeekAndDestroy.Api/SeekAndDestroy.Api.csproj -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app/publish .
ENV ASPNETCORE_URLS=http://+:5090
ENV AiService__BaseUrl=http://ai-service:8088
EXPOSE 5090
ENTRYPOINT ["dotnet", "SeekAndDestroy.Api.dll"]
