# cs-relay — 跨盘转存深圳中继

跨盘转存（夸克 → 115）在国内链路上跑：插件只在中继服务「115 秒传确认未命中」时把**单个文件**交过来，
中继自己解析夸克直链、多连接分段下载、再走阿里云**内网** OSS 分片并发上传 115。
上传完返回结果，插件继续做重命名与整理。

默认德国链路完全不变；中继不可用时按插件开关回落本机。

## 组成

单个容器：任务队列 + 夸克取链 + 多连接分段下载 + 115 上传，无外部下载器依赖。

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
| 上传速度仍低 | 公共端点未被改写 | 检查日志里 `oss-cn-shenzhen-internal`；确认 ECS 与 115 OSS 同地域 |
| 任务卡在 download | 夸克直链过期/限速 | `DELETE /jobs/{id}` 后重试；可调大 `DOWNLOAD_CONNECTIONS` |

## 文档

- 方案：`docs/plan/2026-10-17-深圳中继方案.md`
- 测试：`docs/test/2026-10-17-中继单测.md`
- 部署记录：`docs/summary/2026-10-18-部署记录.md`
