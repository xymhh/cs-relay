# cs-relay — 跨盘转存深圳中继

跨盘转存（夸克 → 115）在国内链路上跑：插件只在中继服务「115 秒传确认未命中」时把**单个文件**交过来，
中继自己解析夸克直链、用**内置多线程 Range 分片**下载、再走阿里云**内网** OSS 分片并发上传 115。
上传完返回结果，插件继续做重命名与整理。

默认德国链路完全不变；中继不可用时按插件开关回落本机。

## 组成

| 容器 | 作用 | 端口 |
| --- | --- | --- |
| `cs-relay` | 任务队列 + 夸克取链 + 多线程下载 + 115 上传 | `8787`（需鉴权） |

0.7.0 起**不再有 Gopeed 容器**：夸克直链支持 Range，中继自己用线程池并发分段拉取即可，
请求头（cookie / UA / Referer）完全可控，失败原因是可读的错误文本而不是一句 `status=error`。

## 下载腿：内置多线程（唯一方式）

`relay/downloader.py`：

1. `Range: bytes=0-0` 探测总大小与是否支持分段；
2. 按固定分片（默认 8 MB）切分，线程池**并发 32 条**连接（可配到 128）；
3. `os.pwrite` 按偏移写入同一文件描述符，避免多线程 seek 竞争；
4. 任一分片失败按次数退避重试，仍失败则整体报错并清理；
5. 校验总大小 + SHA1，全程支持取消与进度回调。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `RELAY_DOWNLOAD_CONNECTIONS` | 32 | 单任务并发连接数（1–128） |
| `RELAY_DOWNLOAD_CHUNK_MB` | 8 | 每个 Range 分片大小（1–64 MB） |

任务级覆盖：`POST /jobs` 请求体可带 `"connections": N`（1–128），该任务就用 N 条连接；
缺省或非法值＝用 `RELAY_DOWNLOAD_CONNECTIONS`。任务开始时日志会打
`（线程=N，任务指定/中继默认）`。

实测（深圳 ECS → 夸克）：4 并发约 7.9 MB/s，16/32 并发降到约 7.3/6.7 MB/s，
瓶颈在 VPS 上行约 2.5 MB/s —— 32 条连接足够跑满，再多只增加对端压力。

## 部署（深圳 ECS）

```bash
# 1. 放代码（git clone 或上传本目录）
cd /opt && git clone <仓库地址> cs-relay && cd cs-relay

# 2. 配置
cp .env.example .env && vi .env      # 填 RELAY_TOKEN / QUARK_COOKIE / P115_COOKIE

# 3. 启动（只剩一个容器，不需要 Gopeed）
docker compose up -d --build

# 4. 自检
curl -s http://127.0.0.1:8787/healthz
curl -s -H "X-Token: $RELAY_TOKEN" http://127.0.0.1:8787/debug/selfcheck
```

`healthz` 返回 `{"ok": true, ...}` 即可。第一次任务前建议先跑一次 1 GB 级小文件验证 115 上传，
或直接 `POST /debug/download {"file_id":"..."}` 测下载速度。

### 从带 Gopeed 的旧版本升级

```bash
cd /opt/csrelay
# 拉新代码（见下节一条命令）
docker compose rm -sf gopeed          # 停并删除旧 Gopeed 容器
# .env 里 GOPEED_* 与 RELAY_DOWNLOAD_ENGINE / QUARK_EXT_AUTO 可以直接删掉（不再读取）
docker compose up -d --build
```

旧 Gopeed 的 `./downloads`、`./storage` 目录不再使用，确认无残留后可直接删除
（中继自己的临时文件在 `data/downloads/<job_id>/`，任务结束即删）。

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
- `POST /jobs`（`X-Token`）：`{"provider":"quark","file_id":"...","file_name":"...","size":123,"sha1":"...","target":{"path":"/整理/待整理","name":"..."},"connections":32}`
- `GET /jobs/{id}`：`status` ∈ queued/running/success/failed/cancelled，`phase` ∈ resolve/download/upload/done，附 `progress/transferred/speed`
- `DELETE /jobs/{id}`：取消（在下载阶段生效）

### 调试接口（`X-Token`，只回脱敏信息）

| 接口 | 作用 |
| --- | --- |
| `GET /debug/status` | 版本、磁盘剩余、下载腿配置、cookie/token 只报有无、最近 5 个任务 |
| `GET /debug/logs?lines=200` | 最近日志（环形缓冲，含 `[access]` 访问日志与任务起止/失败原因） |
| `GET /debug/selfcheck` | 自检：磁盘写 1 MB、凭证是否配置、下载腿并发与分片 |
| `POST /debug/resolve` | 传 `{"file_id":"..."}`：解析夸克直链并真取 64 KB，回报域名/状态码/字节数（不回直链与 cookie） |
| `POST /debug/download` | 传 `{"file_id":"...","seconds":8,"connections":32}`：真实跑一次内置多连接下载，按秒截断并回报实测 MB/s |

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
| `直链返回 403` | cookie 过期或直链被限 | 重导 cookie；`/debug/resolve` 单独验证直链 |
| `直链返回 404` | 源文件已删除/移动 | 换源或跳过 |
| `分片多次失败` | 对端限速/网络抖动 | 降 `RELAY_DOWNLOAD_CONNECTIONS` 到 8–16 后重试 |
| 上传速度仍低 | 公共端点未被改写 | 检查日志里 `oss-cn-shenzhen-internal`；确认 ECS 与 115 OSS 同地域 |
| 任务卡在 download | 夸克直链过期/限速 | `DELETE /jobs/{id}` 后重试；确认源文件仍可下载 |

## 文档

- 方案：`docs/plan/2026-10-17-深圳中继方案.md`
- 测试：`docs/test/2026-10-17-中继单测.md`
- 本次改造：`docs/summary/2026-10-25-中继移除gopeed改内置多线程.md`
