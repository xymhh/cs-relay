# cs-relay — 跨盘转存深圳中继

跨盘转存（夸克 → 115）在国内链路上跑：插件只在中继服务「115 秒传确认未命中」时把**单个文件**交过来，
中继自己解析夸克直链、用 Gopeed 多连接下载、再走阿里云**内网** OSS 分片并发上传 115。
上传完返回结果，插件继续做重命名与整理。

默认德国链路完全不变；中继不可用时按插件开关回落本机。

## 组成

| 容器 | 作用 | 端口 |
| --- | --- | --- |
| `cs-relay` | 任务队列 + 夸克取链 + 115 上传 | `8787`（需鉴权） |
| `cs-gopeed` | 夸克多连接下载（仅内网可达） | 不映射到宿主机 |


### 下载腿：内置引擎（默认）与 Gopeed

`RELAY_DOWNLOAD_ENGINE=builtin`（默认）走 `relay/downloader.py`：单进程线程池 +
Range 分片（8–16 并发），请求头（cookie/UA/Referer）完全由中继自己带，
**不依赖 Gopeed 及其扩展**。Gopeed 的夸克直链下载常常因为缺扩展/丢请求头直接
`status=error`，而失败原因又不透明。

`RELAY_DOWNLOAD_ENGINE=gopeed`（0.4.0 起推荐用于夸克源）走 Gopeed + 夸克扩展：

- 中继启动时自动下载并安装扩展 **igwkang/gopeed-extension-quark**（devMode 文件夹方式），
  并把 `QUARK_COOKIE` 写进扩展设置（`QUARK_EXT_AUTO=1` 控制，失败只记日志不影响启动）；
- 任务 URL 用扩展支持的网盘目录链接 `https://pan.quark.cn/list#/list/all/{file_id}`，
  **中继不再解析直链**，cookie / UA / stoken 全由扩展内部处理；
- 插件若直接给出 `source_url`（分享链接），中继原样透传给扩展。

对照接口：`POST /debug/gopeed/install-quark`（手动重装扩展+写 cookie）、
`GET|POST /debug/gopeed/proxy`（透传 `/api/v1/*`，用于远程查扩展列表与设置）。

内置引擎行为：先 `Range: bytes=0-0` 探总大小与分段能力 → 按 8 MB 分片并发拉取 →
`os.pwrite` 按偏移写入 → 校验总大小与 SHA1；403/404 会直接抛成「直链返回 403（cookie 失效或请求头不够）」
这类可读错误，而不是一句 `status=error`。

## 部署（深圳 ECS）

```bash
# 1. 放代码（git clone 或上传本目录）
cd /opt && git clone <仓库地址> cs-relay && cd cs-relay

# 2. 配置
cp .env.example .env && vi .env      # 填 RELAY_TOKEN / QUARK_COOKIE / P115_COOKIE

# 3. 启动
docker compose up -d --build

# 4. 自检
curl -s http://127.0.0.1:8787/healthz
docker compose logs --tail=50 relay
```

`healthz` 返回 `{"ok": true, ...}` 即可。第一次任务前建议先跑一次 1 GB 级小文件验证 115 上传。

Gopeed 容器（`liwei2633/gopeed`）用 `./downloads:/app/Downloads` + `./storage:/app/storage`，
凭据从 `.env` 注入：`GOPEED_USERNAME` / `GOPEED_PASSWORD` / `GOPEED_APITOKEN`，
其中 `GOPEED_TOKEN`（中继调用用）必须与 `GOPEED_APITOKEN` 填同一个值。

### 升级（部署目录 `/opt/csrelay`，一条命令）

```bash
cd /opt/csrelay
curl -fsSL -o /tmp/cs.tar.gz https://ghfast.top/https://github.com/xymhh/cs-relay/archive/refs/heads/main.tar.gz \
  && tar -xzf /tmp/cs.tar.gz --strip-components=1 \
  && docker compose up -d --build \
  && curl -s http://127.0.0.1:8787/debug/status
```

`.env`、`data/` 不在仓库里，覆盖解压不会动它们。`/debug/status` 里的 `version` 即当前版本。

## 插件侧配置（MP → 网盘订阅助手 → 设置）

| 配置 | 值 |
| --- | --- |
| 国内中继上传 | 开 |
| 中继地址 | `http://<ECS 公网 IP 或域名>:8787` |
| 中继令牌 | 与 `.env` 的 `RELAY_TOKEN` 一致 |
| 单文件上限(GB) | 25（35 GB 盘留余量） |
| 单任务超时(分钟) | 180 |
| 失败是否回落本机 | 开（先观察，稳定后可关） |

## 接口

- `GET /healthz`（免鉴权）
- `POST /jobs`（`X-Token`）：`{"provider":"quark","file_id":"...","file_name":"...","size":123,"sha1":"...","target":{"path":"/整理/待整理","name":"..."}}`
- `GET /jobs/{id}`：`status` ∈ queued/running/success/failed/cancelled，`phase` ∈ resolve/download/upload/done，附 `progress/transferred/speed`
- `DELETE /jobs/{id}`：取消（在下载阶段生效）

### 调试接口（`X-Token`，只回脱敏信息）

| 接口 | 作用 |
| --- | --- |
| `GET /debug/status` | 版本、磁盘剩余、配置是否真的生效（cookie/token 只报有无）、最近 5 个任务 |
| `GET /debug/logs?lines=200` | 最近日志（环形缓冲，含 `[access]` 访问日志与任务起止/失败原因） |
| `GET /debug/selfcheck` | 自检：磁盘写 1 MB、Gopeed `/api/v1/info`、三项凭证是否配置 |
| `POST /debug/resolve` | 传 `{"file_id":"..."}`：解析夸克直链并真取 64 KB，回报域名/状态码/字节数（不回直链与 cookie） |
| `POST /debug/gopeed/install-quark` | 下载/安装夸克扩展并把 `QUARK_COOKIE` 写进扩展设置 |
| `GET /debug/gopeed/proxy?path=/api/v1/extensions` | 透传读 Gopeed 接口（远程排查扩展/任务） |

`/debug/*` 不返回 cookie、直链、令牌；访问日志会记录每个请求（401 也记）。

## 运维

```bash
docker compose ps
docker compose logs -f relay
curl -s -H "X-Token: $RELAY_TOKEN" http://127.0.0.1:8787/jobs | python3 -m json.tool
docker compose restart relay
```

磁盘：`./data/downloads/<job_id>/` 是临时文件，任务结束即删；`./data/jobs.json` 保存最近任务。

## 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动即退出，日志 `RELAY_TOKEN 未设置` | `.env` 缺项 | 补齐三个必填项 |
| 插件日志 `连接中继失败` | 安全组/防火墙未放行 8787 | 放行来源 IP（建议只放德国 MP 出口） |
| `中继鉴权失败` | 令牌不一致 | 对齐 `.env` 与插件配置 |
| `夸克未返回下载地址` | 夸克 cookie 失效 | 重新导出 cookie 写回 `.env` 后 `docker compose up -d` |
| `Gopeed 鉴权失败` | Gopeed 开了 token | `.env` 补 `GOPEED_TOKEN` |
| 上传速度仍低 | 公共端点未被改写 | 检查日志里 `oss-cn-shenzhen-internal`；确认 ECS 与 115 OSS 同地域 |
| 任务卡在 download | 夸克直链过期/限速 | `DELETE /jobs/{id}` 后重试；确认源文件仍可下载 |

## 文档

- 方案：`docs/plan/2026-10-17-深圳中继方案.md`
- 测试：`docs/test/2026-10-17-中继单测.md`
