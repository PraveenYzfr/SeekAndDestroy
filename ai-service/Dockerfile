# Pinned to Debian 12 (bookworm) explicitly, matching the packages.microsoft.com/.../debian/12/...
# repo config below - "python:3.12-slim" now floats onto Debian 13 (trixie), whose apt
# rejects Microsoft's still-SHA1-signed bookworm repo key under trixie's stricter OpenPGP
# policy (no Debian 13 msodbcsql18 repo exists yet to switch to instead).
FROM python:3.12-slim-bookworm

# ODBC Driver 18 for SQL Server (Debian/Ubuntu apt repo)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg2 ca-certificates gcc g++ unixodbc-dev \
    && curl -sSL -O https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
    && dpkg -i packages-microsoft-prod.deb && rm packages-microsoft-prod.deb \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV SAD_SERVICE__HOST=0.0.0.0
ENV SAD_SERVICE__PORT=8088
ENV SAD_DB__ODBC_DRIVER="ODBC Driver 18 for SQL Server"
ENV SAD_DB__TRUST_SERVER_CERTIFICATE=true

EXPOSE 8088
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088"]
