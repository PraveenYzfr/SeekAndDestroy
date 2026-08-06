FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY . .
RUN dotnet restore SeekAndDestroy.slnx
RUN dotnet publish SeekAndDestroy.Api/SeekAndDestroy.Api.csproj -c Release -o /app/publish --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app/publish .
ENV ASPNETCORE_URLS=http://+:5090
ENV AiService__BaseUrl=http://ai-service:8088
EXPOSE 5090
ENTRYPOINT ["dotnet", "SeekAndDestroy.Api.dll"]
