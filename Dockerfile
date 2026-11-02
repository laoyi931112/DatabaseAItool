FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.12-slim-bookworm

# 网络受限时可切换 Debian 软件源，例如：
#   docker build --build-arg APT_MIRROR=mirrors.huaweicloud.com -t database-ai-tool .
ARG APT_MIRROR=deb.debian.org
# 不需要 SQL Server 支持时设为 0，跳过 Microsoft ODBC 驱动安装。
ARG INSTALL_MSSQL_ODBC=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data

# unixODBC + Microsoft ODBC Driver 18：SQL Server（pyodbc）需要。
# Oracle 使用 oracledb thin 模式，MySQL/PostgreSQL/MongoDB 均为纯 Python 驱动，无需额外客户端。
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        if [ -f "$f" ]; then \
            sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" "$f"; \
        fi; \
    done; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    if [ "${INSTALL_MSSQL_ODBC}" = "1" ]; then \
        apt-get install -y --no-install-recommends unixodbc; \
        curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
            -o /tmp/packages-microsoft-prod.deb; \
        dpkg -i /tmp/packages-microsoft-prod.deb; \
        rm -f /tmp/packages-microsoft-prod.deb; \
        apt-get update; \
        ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data /app

USER app

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "app"]
