# DatabaseAItool

给第三方 AI Agent 使用的**只读**数据库 MCP 工具，附带本地管理界面。

- 多数据库：MySQL / MariaDB、PostgreSQL、SQLite、SQL Server、Oracle、MongoDB
- HTTP 隧道：MySQL / MariaDB、PostgreSQL 可复用 Navicat 官方 `ntunnel_*.php` 脚本连接内网数据库
- 管理界面：维护数据库连接、测试连接、创建/编辑/禁用/重置 API Key
- 按 Key 授权：每个 Key 只能访问被勾选的数据库
- 本地存储：连接信息保存在本机 SQLite，数据库密码使用 Fernet 加密，Key 只保存 HMAC 哈希
- 只读保证：工具只提供读取能力，SQL 白名单校验 + 数据库层只读 + 行数/超时限制
- MCP 传输：HTTP Streamable（`/mcp`，Bearer Key）与 stdio（`MCP_API_KEY` 环境变量）

> 设计上**不提供**任何写入、DDL、建库/建表/改字段、删除数据的能力。

## 快速开始

### 方式一：Docker（推荐）

需要 Docker 20.10+。镜像已包含 SQL Server 的 ODBC Driver 18；Oracle 使用 thin 模式，无需安装数据库客户端。

```bash
# 1. 构建镜像
docker build -t database-ai-tool .

# 2. 启动容器（数据持久化，仅本机可访问）
docker run -d --name database-ai-tool \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v database-ai-tool-data:/data \
  -e ADMIN_PASSWORD='换成你的强密码' \
  database-ai-tool

# 3. 检查状态
docker ps
docker logs -f database-ai-tool
curl http://127.0.0.1:8000/healthz
```

打开 <http://127.0.0.1:8000>，用户名 `admin`，密码为启动时设置的 `ADMIN_PASSWORD`。

**局域网 / 远程访问**（必须设置密码，并配置客户端实际使用的 Host）：

```bash
docker run -d --name database-ai-tool \
  --restart unless-stopped \
  -p 8000:8000 \
  -v database-ai-tool-data:/data \
  -e ADMIN_PASSWORD='换成你的强密码' \
  -e MCP_ALLOWED_HOSTS='192.168.1.10:8000,dbtool.example.com' \
  database-ai-tool
```

- `MCP_ALLOWED_HOSTS` 要填写 MCP 客户端访问时 `Host` 头的值（IP/域名，带端口），否则 MCP 会返回 `421 Invalid Host header`。
- 生产环境请放在 HTTPS 反向代理之后，不要把端口直接暴露到公网。

**docker compose**（保存为 `docker-compose.yml`，执行 `docker compose up -d`）：

```yaml
services:
  database-ai-tool:
    build: .
    container_name: database-ai-tool
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      ADMIN_PASSWORD: "换成你的强密码"
      # MCP_ALLOWED_HOSTS: "192.168.1.10:8000"
      DEFAULT_MAX_ROWS: "500"
      HARD_MAX_ROWS: "5000"
    volumes:
      - database-ai-tool-data:/data

volumes:
  database-ai-tool-data:
```

> 容器内监听 `0.0.0.0:8000`，能否被外部访问由 `-p` 的绑定地址决定；`-p 127.0.0.1:8000:8000` 只允许本机访问。

**构建失败：`Temporary failure resolving 'deb.debian.org'`**

这是 Docker 构建环境无法解析域名，与项目代码无关。按顺序排查：

1. 确认容器 DNS 是否正常：

   ```bash
   docker run --rm busybox nslookup deb.debian.org
   ```

   解析失败时，在 Docker Desktop → Settings → Docker Engine 中加入公共 DNS，保存并重启 Docker：

   ```json
   {
     "dns": ["223.5.5.5", "119.29.29.29"]
   }
   ```

   Linux 服务器可将同样内容写入 `/etc/docker/daemon.json`，然后执行 `sudo systemctl restart docker`。

2. 仍失败时，改用国内 Debian 镜像源构建：

   ```bash
   docker build \
     --build-arg APT_MIRROR=mirrors.huaweicloud.com \
     -t database-ai-tool .
   ```

   `APT_MIRROR` 也可换成 `mirrors.aliyun.com`、`mirrors.tuna.tsinghua.edu.cn`、`mirrors.ustc.edu.cn`、`mirrors.cloud.tencent.com`；可用 `docker run --rm busybox nslookup mirrors.huaweicloud.com` 先确认能解析。

3. 公司网络需要代理时，在 Docker Desktop 中配置代理，或构建时传入：

   ```bash
   docker build \
     --build-arg HTTP_PROXY=http://代理地址:端口 \
     --build-arg HTTPS_PROXY=http://代理地址:端口 \
     -t database-ai-tool .
   ```

4. 只使用 MySQL / PostgreSQL / SQLite / Oracle / MongoDB 时，可以跳过 Microsoft ODBC 驱动，减少一个外网依赖：

   ```bash
   docker build \
     --build-arg APT_MIRROR=mirrors.huaweicloud.com \
     --build-arg INSTALL_MSSQL_ODBC=0 \
     -t database-ai-tool .
   ```

**数据持久化与备份**：容器内数据目录为 `/data`（保存 `app.db` 与 `secret.key`），已挂载到命名卷。备份：

```bash
docker run --rm -v database-ai-tool-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/database-ai-tool-backup.tgz -C /data .
```

**连接宿主机上的 SQLite 文件**：容器内的相对路径指向容器内部。需要把宿主机目录挂载进去，以只读方式挂载：

```bash
docker run ... -v /宿主机/数据库目录:/db:ro ... database-ai-tool
# 管理界面里的 SQLite 路径填写 /db/example.db
```

### 方式二：本地 Python

需要 Python 3.11+。

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env        # Windows；Linux/macOS 用 cp
python -m app
```

打开 <http://127.0.0.1:8000>。

默认监听 `127.0.0.1:8000`。建议在 `.env` 中设置 `ADMIN_PASSWORD`，否则管理界面在本机不需要登录。

## 使用步骤

1. **启动服务**：按上面的 Docker 或本地方式启动，确认 `curl http://127.0.0.1:8000/healthz` 返回 `{"status":"ok"}`。
2. **登录管理界面**：浏览器打开 <http://127.0.0.1:8000>。设置了 `ADMIN_PASSWORD` 时输入用户名（默认 `admin`）和密码。
3. **添加数据库连接**：进入「数据库连接」→「新增连接」，选择数据库类型，填写主机、端口、库名、账号密码；SQLite 只需填写文件的绝对路径。高级选项（JSON）按需填写，见下文。
4. **测试连接**：保存后在列表中点「测试」，确认显示连接成功。连接创建后仍可编辑；密码留空表示不修改。
5. **创建 API Key**：进入「API Keys」→「新建 Key」，勾选该 Key 允许访问的数据库，可设置过期时间。创建后立即复制完整 Key（`dbt_...`，只显示这一次）。
6. **接入第三方 Agent**：把 MCP 端点 `http://127.0.0.1:8000/mcp` 和 Key 填入 Agent 配置，参考下一节。
7. **日常维护**：可随时禁用/启用 Key、重置 Key（旧 Key 立即失效）、修改数据库授权；不用的连接和 Key 可直接删除。Key 列表会显示最近使用时间。

## MCP 接入

- 端点：`http://127.0.0.1:8000/mcp`（远程部署时换成实际地址）
- 鉴权：`Authorization: Bearer dbt_...`，或 `X-API-Key: dbt_...`
- 传输：Streamable HTTP

> 通过域名/反向代理访问时，域名必须加入 `MCP_ALLOWED_HOSTS`，否则返回 `421 Invalid Host header`。如果反向代理已负责访问控制，也可以设置 `MCP_DISABLE_HOST_CHECK=true` 彻底关闭该校验（API Key 鉴权仍然生效）。

支持 Streamable HTTP 的客户端配置示例：

```json
{
  "mcpServers": {
    "database-ai-tool": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer dbt_你的Key"
      }
    }
  }
}
```

只支持 stdio 的桌面客户端可以用桥接：

```bash
npx -y mcp-remote http://127.0.0.1:8000/mcp --header "Authorization: Bearer dbt_你的Key"
```

也可以在本机以 stdio 模式运行（需要先启动过一次管理界面，确保 Key 已创建）：

```bash
MCP_API_KEY=dbt_你的Key python -m app.mcp.stdio
```

### MCP 工具

| 工具 | 作用 |
| --- | --- |
| `list_databases` | 列出该 Key 可访问的数据库 |
| `get_database_info` | 数据库类型、版本、默认 schema |
| `list_schemas` | 列出 schema（MySQL 为数据库，MongoDB 为配置的库） |
| `list_tables` | 列出表/视图/集合 |
| `describe_table` | 字段、类型、主键、外键、索引（MongoDB 采样推断） |
| `sample_rows` | 读取少量样例行/文档 |
| `query` | 执行单条只读 SELECT（SQL 数据库） |
| `find_documents` | MongoDB 只读 find |
| `aggregate_documents` | MongoDB 只读聚合；禁止 `$out` / `$merge` |

典型调用顺序：`list_databases` → `list_tables` → `describe_table` → `query` / `sample_rows`。

## 支持的数据库

| 类型 | 驱动 | 只读保障 |
| --- | --- | --- |
| SQLite | 内置 | 以 `mode=ro` URI 打开文件 |
| PostgreSQL | `psycopg` | 会话设置 `default_transaction_read_only=on` |
| MySQL / MariaDB | `PyMySQL` | 会话设为 `TRANSACTION READ ONLY`（需 MySQL ≥ 5.7 / MariaDB ≥ 10.0） |
| SQL Server | `pyodbc` | SQL 白名单 + 建议使用只读账号（无会话级只读） |
| Oracle | `oracledb`（thin 模式，无需客户端） | 查询前执行 `SET TRANSACTION READ ONLY` + 白名单 |
| MongoDB | `pymongo` | 只暴露 find / 只读聚合，拒绝 `$out`、`$merge`；建议只读账号 |

SQL Server 需要安装对应的 ODBC 驱动（默认 `ODBC Driver 18 for SQL Server`），或在高级选项中指定 `driver`。

## 只读安全模型

1. **能力层**：MCP 只注册读取类工具，没有任何写入工具。
2. **SQL 校验**（`app/services/query_guard.py`）：
   - 使用 `sqlglot` 解析，只允许单条 `SELECT` / `WITH ... SELECT` / `UNION` / `INTERSECT` / `EXCEPT`
   - 拒绝 INSERT / UPDATE / DELETE / DDL / `SELECT INTO` / 锁查询 / 多语句 / `PRAGMA` / `ATTACH` 等
   - 拒绝可执行注释 `/*! ... */`（MySQL）与一批危险函数（如 `pg_read_file`、`set_config`、`DBMS_*`、`UTL_*`）
   - MySQL / SQL Server 额外限制不能跨库引用
3. **数据库层**：
   - SQLite 文件只读打开；PostgreSQL / MySQL 会话只读；Oracle 查询事务只读
   - 结果按连接配置的最大行数截断；查询/连接设置超时
4. **Key 控制**：Key 只保存 HMAC-SHA256 哈希，可随时禁用、设过期时间、重置或删除；每次 MCP 请求校验 Key，每次工具调用再次校验 Key 状态与数据库授权。
5. **管理界面**：密码 Fernet 加密；可选 HTTP Basic 管理员认证；POST 请求做同源校验（防 CSRF）；默认只监听本机。

### 重要建议

- **为每个数据库使用只读数据库账号**。应用层防护不能替代数据库权限，尤其是 SQL Server / Oracle 以及同一连接内跨 schema 访问的场景。
- 生产环境不要直接暴露本服务：默认只监听 `127.0.0.1`；如需远程访问，请放在 HTTPS 反向代理后，并设置 `ADMIN_PASSWORD`，同时把域名/地址加入 `MCP_ALLOWED_HOSTS`。
- `data/secret.key` 用于解密数据库密码，`data/app.db` 保存连接与 Key 哈希。请备份这两个文件；丢失 `secret.key` 后将无法恢复已保存的数据库密码。

## 高级连接选项（JSON）

表单中的「高级选项」会按数据库类型传给驱动：

```jsonc
// PostgreSQL：SSL / 额外连接参数
{"query": {"sslmode": "require"}}

// MySQL：字符集 / 自定义驱动参数
{"charset": "utf8mb4", "connect_args": {"ssl": {"ca": "/path/ca.pem"}}}

// SQL Server：指定 ODBC 驱动并信任自签证书
{"driver": "ODBC Driver 18 for SQL Server", "trust_server_certificate": true}

// MongoDB：认证源 / TLS
{"authSource": "admin", "tls": true}

// MongoDB：直接使用连接串（Atlas 等）
{"uri": "mongodb+srv://user:pass@cluster.example.net/?retryWrites=true"}
```

其中 `query` 会作为 SQLAlchemy URL 查询参数，`connect_args` 会作为 DBAPI 连接参数。

## 通过 Navicat HTTP 隧道连接

当数据库只能从内网访问，但有一台能访问数据库的 PHP 主机时，可以复用 Navicat 的 HTTP 隧道：把官方脚本 `ntunnel_mysql.php`（MySQL / MariaDB）或 `ntunnel_pgsql.php`（PostgreSQL）上传到该 PHP 站点，本工具直接以 Navicat 隧道协议访问脚本，不需要安装 Navicat 客户端。

添加连接时选择 MySQL / PostgreSQL，然后在「HTTP 隧道」区域填写：

| 字段 | 说明 |
| --- | --- |
| 隧道地址 | 脚本的完整 URL，例如 `https://example.com/ntunnel_mysql.php` |
| 隧道用户名 / 密码 | 可选，HTTP Basic 认证（脚本本身或前置反向代理启用时填写），密码加密保存 |
| Base64 编码查询 | 默认开启，与 Navicat 客户端默认行为一致；脚本端 `$encodeBase64` 关闭时需取消勾选 |

主机、端口、库名、账号密码仍然填数据库本身的信息——它们由隧道脚本使用，因此应填写**脚本所在主机能访问到的**数据库地址。勾选隧道后端口留空会按类型补默认值（MySQL 3306 / PostgreSQL 5432）。

注意：

- 隧道只支持 MySQL / MariaDB 与 PostgreSQL；SQLite、SQL Server、Oracle、MongoDB 仍需直连。
- 隧道同样受只读保护：SQL 白名单校验 + 脚本会话设置只读。由于隧道在单个 HTTP 响应中返回全部结果，查询会自动改写为 `SELECT * FROM (<原语句>) AS _aitool LIMIT n+1` 在服务端截断行数。
- 隧道传输的是数据库凭据和查询结果（Base64 可解码），请务必使用 HTTPS 并限制脚本访问权限。

## 配置项（`.env`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址；Docker 镜像内默认 `0.0.0.0` |
| `PORT` | `8000` | 监听端口 |
| `ADMIN_USERNAME` | `admin` | 管理界面 Basic 认证用户名 |
| `ADMIN_PASSWORD` | 空 | 设置后管理界面需要登录；为空则本机免登录 |
| `DATA_DIR` | `./data` | 内部 SQLite 与密钥文件目录；Docker 镜像内为 `/data` |
| `MCP_ALLOWED_HOSTS` | 空 | 额外允许的 Host（逗号分隔），用于非 localhost 访问 |
| `MCP_DISABLE_HOST_CHECK` | `false` | 关闭 MCP 的 Host/Origin 校验，仅建议在可信反向代理后使用 |
| `DEFAULT_MAX_ROWS` | `500` | 新建连接的默认返回行数上限 |
| `HARD_MAX_ROWS` | `5000` | 允许配置的绝对上限 |
| `DEFAULT_QUERY_TIMEOUT` | `30` | 新建连接的默认超时（秒） |
| `CONNECT_TIMEOUT` | `5` | 建立数据库连接超时（秒） |

## 目录结构

```text
app/
  adapters/          # SQL / MongoDB 适配器与只读连接
  services/          # 连接与 Key 业务、SQL 只读校验
  mcp/               # MCP 工具、HTTP/stdio 传输、Key 鉴权
  web/               # FastAPI 管理界面、模板与静态资源
  config.py          # 配置
  models.py          # 内部 SQLite 模型
  security.py        # 密码加密、Key 哈希
tests/               # pytest 测试
data/                # 运行时生成（已 gitignore）
Dockerfile           # 容器镜像（含 SQL Server ODBC Driver 18）
.dockerignore
docker-compose.yml   # 可选，自行按 README 示例创建
```

## 测试

```bash
pytest
```

测试覆盖 SQL 只读校验、加密与 Key 哈希、SQLite 只读连接与查询、MongoDB 写阶段拦截、Key 权限、管理界面流程、MCP 鉴权与完整协议调用。

## 已知边界

- 只允许单条 `SELECT`；`SHOW` / `EXPLAIN` / `DESCRIBE` 不作为 `query` 放行，请使用元数据工具。
- 同一数据库连接内的跨 schema 访问由数据库账号权限决定（MySQL/SQL Server 已做跨库名限制）。需要严格隔离时请使用独立只读账号。
- SQL Server 没有稳定的会话级只读开关，务必配合只读账号使用。
- MongoDB 的 `$function`/`$where` 等服务器端 JS 不用于写库，但可能消耗资源；请通过只读账号和超时控制风险。
- 本服务不内置 TLS；远程部署请使用反向代理。

## 扩展新的数据库类型

在 `app/adapters/` 中实现与 `SQLAdapter` / `MongoAdapter` 相同的方法（`test_connection`、`get_database_info`、`list_schemas`、`list_tables`、`describe_table`、`sample_rows`、`query`），在 `DB_TYPE_INFO` 与 `get_adapter` 中注册，即可接入界面、MCP 与 Key 授权体系。
