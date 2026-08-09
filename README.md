# JJboneTv

JJboneTv 是一个面向文本、图片、视频和音频生成的画布工作台。当前版本包含用户私有的文件夹与项目库、完整画布持久化、历史版本、统一资产模型，以及可扩展的生成任务 API。

## 技术架构

- 前端：React 19 + Vite，最终运行在 Nginx 容器中
- 后端：Python 3.12 + FastAPI + SQLAlchemy
- 数据库：PostgreSQL 16
- 用户端入口：`http://localhost:3001`
- 管理员端入口：`http://localhost:3002`

数据库结构由 Alembic 管理。Compose 使用一次性的 `migration` 服务执行 `alembic upgrade head`；迁移成功后用户 API、管理员 API 和生成 Worker 才会启动。API 容器本身不执行迁移，也不依赖 SQLAlchemy `create_all()` 修改已有表。

## 启动

仓库不使用默认 `.env`。开发和生产命令必须显式指定环境文件，防止 Docker Compose 静默加载错误环境：

```bash
cp .env.development.example .env.development
docker compose --env-file .env.development up --build -d
```

真实的 `.env.development` 和 `.env.production` 均被 Git 忽略，仓库只保留带注释的模板。用户前端默认映射宿主机 3001 端口，管理员前端默认映射 3002 端口；需要覆盖时修改当前环境文件中的 `FRONTEND_PORT` 和 `ADMIN_FRONTEND_PORT`。

查看服务状态：

```bash
docker compose --env-file .env.development ps
curl http://localhost:3001/api/health
curl http://localhost:3002/admin-api/health
```

### 开发热更新

如果只修改源码，不想每次重新构建镜像，使用开发覆盖配置启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
  --env-file .env.development up -d --build
```

这个命令首次仍需要构建后端依赖；之后修改 `backend/app` 会触发 Uvicorn 自动重载，修改 `frontend` 或 `admin-frontend` 会由 Vite HMR 自动更新。用户端和管理端入口仍分别是 `http://localhost:3001` 和 `http://localhost:3002`，`/api` 与 `/admin-api` 会通过 Vite 代理到对应后端。

Worker 也会监听 Python 文件并自动重启。修改 `requirements.txt`、`package-lock.json`、Dockerfile、系统依赖或环境变量后，仍需重新构建或重启相关服务。`docker-compose.dev.yml` 仅用于开发，生产部署继续使用原来的 `docker-compose.yml`。

`migration` 正常完成后状态会显示为 `Exited (0)`。图片和视频任务分别由不开放端口的 `image-worker`、`video-worker` 持久领取；可以使用以下命令查看迁移和 Worker 日志：

```bash
docker compose --env-file .env.development logs migration image-worker video-worker
```

停止服务：

```bash
docker compose --env-file .env.development down
```

## 接入模型 API

模型接入分为两层：管理员在管理端创建模型分组，并为模型配置供应商模型名与 Base URL；用户在用户端的“模型 API Key”页面按分组配置自己的 API Key。一个模型只能属于一个分组，同一用户对同一分组只保存一份 Key。

API Key 使用独立密钥加密后存入 PostgreSQL，前端只能看到是否已配置、末四位和更新时间。生产部署前必须在 `.env.production` 中设置独立的高强度随机密钥：

```text
USER_MODEL_CREDENTIALS_SECRET=replace-with-a-separate-long-random-secret
```

后端调用供应商时根据用户选择的模型组合 `base_url + model_name + 用户所属分组的 API Key`。只有分组和模型都启用、且当前用户已配置该分组 Key 时，模型才会通过 `/api/models` 出现在该用户的 Studio。修改 `USER_MODEL_CREDENTIALS_SECRET` 会导致已有凭证无法解密，生产环境轮换密钥前必须先完成凭证重加密。

图片模型支持 `openai_images` 协议。模型的 Base URL 可填写 API 根地址，例如 `https://www.thinkai.tv/v1`，后端默认调用其 `/images/generations` 路径；如供应商使用其他路径，可在 Metadata 中设置 `endpoint_path`。示例配置：

```json
{
  "protocol": "openai_images",
  "capabilities": ["text-to-image"]
}
```

`config_schema` 定义允许用户选择的参数及枚举范围，`default_parameters` 提供默认值。后端会合并并校验两者，拒绝未声明参数和越界值。供应商返回的 URL 或 Base64 图片会由后端下载并写入当前环境配置的资产存储；API Key、Base64 原文和供应商临时 URL 不会写入任务结果。

## 阿里云 OSS 资产存储

生成资产支持 `local` 和 `oss` 两种存储后端。OSS Bucket 必须保持私有；用户访问 `/api/assets/{id}/content` 时，后端先验证登录用户和项目归属，再返回有效期有限的 OSS 签名地址。数据库只保存对象键、Bucket 名称和资产元数据，不保存 OSS AccessKey。

开发环境和生产环境使用两套独立环境文件，运行时只加载当前环境的 OSS 凭据：

```bash
# 开发环境
cp .env.development.example .env.development
docker compose --env-file .env.development up --build -d

# 生产环境
cp .env.production.example .env.production
docker compose --env-file .env.production up --build -d
```

两份配置必须使用不同的 Bucket 和不同的 RAM 凭据：

```text
APP_ENVIRONMENT=development
ASSET_STORAGE_BACKEND=oss
OSS_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com
OSS_INTERNAL_ENDPOINT=
OSS_BUCKET_NAME=your-development-bucket
OSS_ACCESS_KEY_ID=your-development-ram-access-key-id
OSS_ACCESS_KEY_SECRET=your-development-ram-access-key-secret
OSS_SECURITY_TOKEN=
OSS_PREFIX=thinktv/generated
OSS_CNAME=false
OSS_SIGNED_URL_EXPIRE_SECONDS=3600
```

- 本地开发通常把 `OSS_INTERNAL_ENDPOINT` 留空；部署在同地域阿里云 ECS 时可配置内网 Endpoint，上传走内网，签名下载仍使用 `OSS_ENDPOINT`。
- 使用自定义域名时，把 `OSS_ENDPOINT` 设置为自定义域名并将 `OSS_CNAME=true`。
- RAM 身份只需授予目标 Bucket 前缀下的 `PutObject`、`GetObject` 和 `DeleteObject`，不要使用阿里云主账号 AccessKey。
- 切换到 OSS 后，新生成图片写入 OSS。已有本地资产仍按其历史元数据从 `generated_assets` Docker 卷读取，因此迁移完成前不要删除该卷。
- 开发数据库和生产数据库应保持隔离。若需要把项目从开发环境发布到生产环境，必须同时复制对应 OSS 对象并更新资产存储元数据。

## 认证配置

认证接口位于 `/api/auth`，使用 HttpOnly Cookie 保存 HS256 JWT。部署前必须在对应的 `.env.development` 或 `.env.production` 中配置密钥，并根据 HTTPS 部署情况启用安全 Cookie：

```text
JWT_SECRET=replace-with-a-long-random-secret
JWT_EXPIRE_MINUTES=1440
AUTH_COOKIE_SECURE=true
```

本地 HTTP 开发环境应保持 `AUTH_COOKIE_SECURE=false`。注册、登录、当前用户和退出接口分别为 `/register`、`/login`、`/me` 和 `/logout`。

注册和登录按同一客户端 IP 共享 PostgreSQL 精确滑动窗口，默认 60 秒最多 5 次。超过限制时返回 `429`、`Retry-After` 和 `Cache-Control: no-store`。只有 `AUTH_TRUSTED_PROXY_CIDRS` 中的代理可以提供 `X-Forwarded-For`；默认 Compose 网络把用户前端 Nginx 固定在 `172.30.0.10` 并只信任该单一地址。改变 Docker 网段、增加代理副本或接入负载均衡器时，必须同步改为实际代理的精确 CIDR，禁止信任 `0.0.0.0/0` 或 `::/0`。

## 管理员端

管理员端与用户端共用 PostgreSQL，但使用独立的 FastAPI 服务、JWT 密钥和 HttpOnly Cookie。管理员可以只读查看用户邮箱及项目统计，并管理用户 Studio 可见的模型目录。管理员端不保存模型 API Key，也不包含模型计价功能。

本地管理员账号由 `.env.development` 中的 Bootstrap 配置创建：

```text
ADMIN_BOOTSTRAP_ENABLED=true
ADMIN_BOOTSTRAP_EMAIL=admin@thinktv.local
ADMIN_BOOTSTRAP_PASSWORD=AdminLocalPassword123
```

“模型分组”用于组织共用同一份用户 API Key 的模型；“模型管理”配置所属分组、供应商模型名、Base URL、展示名称、类型、描述、状态、展示顺序及扩展配置。内部稳定 Key 由后端自动生成，管理员无需填写任何 API Key。用户端接口不会返回 `base_url` 或用户密钥。

管理员端相关环境变量：

```text
ADMIN_FRONTEND_PORT=3002
ADMIN_JWT_SECRET=replace-with-a-different-long-random-secret
ADMIN_JWT_EXPIRE_MINUTES=480
ADMIN_COOKIE_SECURE=false
ADMIN_BOOTSTRAP_ENABLED=true
ADMIN_BOOTSTRAP_EMAIL=admin@thinktv.local
ADMIN_BOOTSTRAP_PASSWORD=replace-with-a-strong-bootstrap-password
```

生产环境默认设置 `ADMIN_BOOTSTRAP_ENABLED=false`，此时邮箱和密码必须为空。首次初始化时临时启用 Bootstrap，使用有效邮箱和至少 16 位、包含大小写字母及数字的密码；账号创建后立即关闭开关并清空邮箱和密码。管理员账号只会在表为空时初始化，后续修改环境变量不会覆盖已有账号。生产环境还必须替换 `ADMIN_JWT_SECRET` 并设置 `ADMIN_COOKIE_SECURE=true`。

## 项目与画布数据

- `/api/folders` 管理当前用户的单层文件夹。
- `/api/projects` 管理根目录或文件夹内的项目。
- `/api/projects/{id}/workflow` 使用 `base_revision` 保存完整画布，并通过 `409` 防止多标签页静默覆盖。
- `/api/projects/{id}/versions` 提供历史版本列表与恢复。
- `/api/assets` 统一管理 `text`、`image`、`video` 和 `audio` 资产。
- `/api/generations` 记录与项目、节点和画布修订关联的生成任务。

创建生成任务必须发送 1–128 位无空白 ASCII `Idempotency-Key`。同一用户用相同 Key 重试相同请求时返回原任务，并带 `Idempotency-Replayed: true`；相同 Key 对应不同请求时返回 `409`。前端应为每次新的用户操作创建新 UUID，同一次网络重试复用原 UUID。

生成接口只把任务提交为 `queued`，独立 Worker 使用 PostgreSQL `FOR UPDATE SKIP LOCKED`、租约和 fencing token 领取执行。Worker 崩溃后任务可在租约过期后恢复，公开状态仍为 `queued`、`running`、`succeeded`、`failed`、`cancelled`。这是至少一次执行模型，不承诺供应商调用严格 exactly-once；供应商不支持幂等时，极端崩溃窗口仍可能重复调用和计费。

供应商主 JSON、URL 媒体和 Base64 解码都受大小限制。主 JSON 和 URL 内容按流式读取的实际解压后字节数停止，但限内 JSON 最终仍会整体解析到内存；这不是恒定内存的事件式 JSON 解析。URL 限长也不等同于 SSRF 防护，生产部署仍应限制供应商地址和出站网络。

浏览器业务数据不再写入 `localStorage`，其中仅保留界面主题偏好。媒体上传目前只提供本地预览；模型生成的图片会写入当前环境配置的本地存储或阿里云 OSS，并通过鉴权后的短期签名 URL 访问。

## 旧数据库迁移

从早期无用户归属的数据库升级时，如果已有 `projects` 数据，必须在运行迁移前设置 `THINKTV_LEGACY_OWNER_ID`。如果还有旧的 `generation_tasks`，还必须设置 `THINKTV_LEGACY_PROJECT_ID`。两个值都必须是数据库中已存在且归属关系正确的 UUID。

```bash
docker compose --env-file .env.development run --rm migration alembic current
docker compose --env-file .env.development run --rm migration alembic check
```

首次生产升级前应备份 PostgreSQL。初始兼容迁移包含旧表原地改造，不提供自动降级。
