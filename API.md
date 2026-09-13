# 后台 API 规格（Endpoint Spec）

本文档定义 **cline-usage-reporter 客户端 ↔ 自建后台**之间的接口契约。

客户端行为已经固定，服务端的设计自由度比通常情况小 —— 很多看似合理的设计（部分成功、4xx 校验、append 流水）都会直接导致**数据丢失或无限重试**。第 1 节列出这些约束及其成因，是整份 spec 的基础。

---

## 目录

- [1. 设计前提](#1-设计前提)
- [2. 通用约定](#2-通用约定)
- [3. POST /api/usage/report](#3-post-apiusagereport)
- [4. POST /api/skill/report](#4-post-apiskillreport)
- [5. GET /api/health](#5-get-apihealth)
- [6. 查询接口](#6-查询接口)
- [7. 幂等与并发](#7-幂等与并发)
- [8. 限流、容量与超时](#8-限流容量与超时)
- [9. 错误码与重试矩阵](#9-错误码与重试矩阵)
- [10. 安全](#10-安全)
- [11. 参考实现](#11-参考实现)
- [12. Schema 演进规则](#12-schema-演进规则)
- [13. 对客户端 API 的改进建议](#13-对客户端-api-的改进建议)

---

## 1. 设计前提

### 1.1 客户端行为约束

以下每一条都来自客户端实现，是**服务端必须满足的硬约束**：

| 客户端行为 | 代码位置 | 对服务端的约束 |
|---|---|---|
| 仅以 HTTP 状态码判断整批成败，**成功响应体被完全忽略** | `cli.py` `cmd_report` | 必须**整批全收或整批拒收**。返回 200 却在 body 里夹带部分失败 → 客户端视为全部成功并标记已上报，**失败记录永久丢失** |
| 收到 `4xx` 立即 `break`，不再尝试 | `reporter.py` `_post` | 4xx 只能用于永久性错误，不能用于"稍后重试可能成功"的场景 |
| 网络错误 / `5xx` 指数退避重试（间隔 1s→2s→4s→8s） | `reporter.py` `_post` | 5xx 可安全使用，客户端会重试 |
| 失败的整批写入 `pending.jsonl`，**下一轮运行**重新上报 | `cli.py` + `state.py` | 4xx **不等于丢弃**：该批会进本地队列，之后每次运行都重试，一直失败就一直重试 |
| 同一任务的 token 随会话推进增长，指纹变化即重报 | `collector.py` `fingerprint` | 必须按自然键 **upsert**，绝不能 append 插入流水 |
| `--force` 会重发**全部历史记录** | `cli.py` | upsert 必须对旧数据幂等，聚合统计必须基于表而非事件流 |
| `mode=single` 时发送**裸记录对象**，没有批量信封 | `reporter.py` `send_payloads` | 同一端点需同时兼容「信封 + records」与「单条记录」两种 body |
| 请求体 `ensure_ascii=False` 编码为 UTF-8，且**不设置 charset** | `reporter.py` `_post` | 必须按 UTF-8 解码；按 latin-1 处理会导致中文任务描述乱码 |
| 不发送 `Accept` 头，不发送 `Idempotency-Key` | `reporter.py` `_headers` | 不得依赖这些头做路由或去重 |
| 默认 200 条/批，失败时**不会自动降批** | `reporter.py` | 请求体上限必须留有足够余量，否则 413 会造成永久失败循环 |

### 1.2 三个最容易踩的坑

**坑一：部分成功即静默丢数据。** 若服务端返回 `200` + `{"rejected": [3 条]}`，客户端会把整批标记为已上报，那 3 条再也不会重发。**要么全收并落库，要么整批返回非 2xx。**

**坑二：4xx 校验会造成无限重试。** 客户端对单个畸形记录无法剔除，只能整批重试。因此对**单条记录**的字段校验失败，应返回 200 并落库/隔离，而不是 400；400 只留给**信封本身不可解析**的情况（那通常是配置或客户端 bug，必须人工介入）。

**坑三：靠路径区分数据类型不可靠。** 当 `skill_path` 未配置时，客户端会把 skill 批次发到 `usage_path`：

```python
def url_for(self, kind: str) -> str:
    key = "skill_path" if kind == "skill" else "usage_path"
    url = endpoint_url(self.config, key)
    if not url and kind == "skill":
        url = endpoint_url(self.config, "usage_path")
    return url
```

所以服务端应**按 body 中的 `report_type` 分派**，而不是按 URL 路径。

---

## 2. 通用约定

### 2.1 传输

| 项 | 约定 |
|---|---|
| 协议 | **HTTPS 强制**。明文 HTTP 会泄露 token 与任务描述 |
| 方法 | `POST`（上报）、`GET`（查询） |
| `Content-Type` | 请求 `application/json`（客户端不带 charset）；响应必须 `application/json; charset=utf-8` |
| 编码 | UTF-8，禁止 ASCII 转义假设 |
| 时间 | 所有时间戳为**毫秒** epoch（`int`）。注意来自客户端本机时钟，未做时区校正 |
| 压缩 | 客户端不发送 `Content-Encoding`，服务端可返回 gzip 压缩响应 |

### 2.2 请求头

| 头 | 是否必带 | 说明 |
|---|---|---|
| `Content-Type` | 是 | 固定 `application/json` |
| `User-Agent` | 是 | 固定 `cline-usage-reporter/1.0` |
| `Authorization` | 视配置 | `Bearer <token>`；客户端会自动补 `Bearer ` 前缀（若 token 已以 `bearer ` 开头则原样使用） |
| 自定义头 | 可选 | 由 `endpoint.headers` 配置，可用于灰度标记、租户路由等 |

> 服务端若需租户隔离，推荐用**独立 token** 而非自定义头 —— token 已经在客户端配置里。

### 2.3 统一响应格式

成功：

```json
{
  "ok": true,
  "accepted": 200,
  "upserted": 200
}
```

失败：

```json
{
  "ok": false,
  "error": {
    "code": "invalid_envelope",
    "message": "records must be a non-empty array",
    "detail": { "field": "records" }
  }
}
```

`error.code` 用稳定的机器可读枚举，`message` 面向排障（客户端会把失败响应体前 500 字符打入日志与 stderr，所以 `message` 要能自解释）。

### 2.4 幂等键

**客户端不上报 `fingerprint`。** 指纹仅存于本地 `<state.dir>/state.json` 与 `pending.jsonl`，不会出现在请求体中。可用字段全部在请求体里，因此服务端有两种选择：

| 方案 | 自然键 | 说明 |
|---|---|---|
| **推荐** | `(terminal_id, ide_type, task_id)` + `ts` 单调守卫 | 简单、稳健，不依赖浮点序列化细节 |
| 备选 | 服务端按客户端算法自行计算 `fingerprint` | 需精确复刻 Python 的 JSON 序列化（见下），跨语言实现极易不一致，**不推荐** |

若选择备选方案，算法为：

```
sha1(json.dumps([
  ts, tokens_in, tokens_out, cache_writes, cache_reads,
  total_cost, size, model, cwd, is_favorited, task_text
], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
```

风险点：`total_cost` 是 Python `float`，`0.0` 的序列化结果与 Java/Go 的默认输出可能不同；`cwd`、`task_text` 为 `null` 时各语言表述一致但需确认。**除非确有需要，请用推荐方案。**

---

## 3. POST /api/usage/report

Token 用量上报。`report_type` 为 `cline_usage_batch`（批量）或 `cline_usage`（单条）。

### 3.1 请求体 — 批量模式（默认）

```json
{
  "report_type": "cline_usage_batch",
  "schema_version": 1,
  "hostname": "Your Mac",
  "terminal_id": "your-mac",
  "sent_at": 1789306691000,
  "records": [
    {
      "report_type": "cline_usage",
      "schema_version": 1,
      "terminal_id": "your-mac",
      "hostname": "Your Mac",
      "ide_type": "vscode",
      "ide_app": "Code",
      "source": "taskHistory.json",
      "task_id": "1787250927116",
      "task_ulid": "01M0G79SGEKY7V2T2PC9V3CP68",
      "task_text": "分析 HR 人才标签管理系统 PRD…",
      "task_text_hash": "7369d5830428956abd822a701d7a1e786e33027a",
      "ts": 1787590155996,
      "model": "your-model",
      "model_provider": "cline",
      "models_used": ["your-model", "your-model-mini"],
      "cwd": "/Users/you/Desktop/TRAE",
      "project": "TRAE",
      "tokens_in": 1249038,
      "tokens_out": 154866,
      "cache_writes": 343807,
      "cache_reads": 11996800,
      "total_cost": 0.20875371,
      "size": 6960138,
      "is_favorited": false,
      "cline_version": "3.76.0",
      "environment": {
        "os_name": "darwin",
        "os_version": "25.4.0",
        "os_arch": "arm64",
        "host_name": "Visual Studio Code",
        "host_version": "1.98.2"
      }
    }
  ]
}
```

**信封字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `report_type` | string | 是 | 恒为 `cline_usage_batch`。**服务端应以此分派路由** |
| `schema_version` | int | 是 | 当前 `1` |
| `hostname` | string | 是 | 机器名，与记录内同名 |
| `terminal_id` | string | 是 | 终端标识，与记录内同名 |
| `sent_at` | int (ms) | 是 | 本批发送时间 |
| `records` | array | 是 | 非空数组，长度 ≤ 客户端 `batch_size`（默认 200） |

**记录字段**

| 字段 | 类型 | 必填 | 来源 | 说明 |
|---|---|---|---|---|
| `report_type` | string | 是 | — | 恒为 `cline_usage` |
| `schema_version` | int | 是 | — | 当前 `1` |
| `terminal_id` | string | 是 | 自动探测 | **业务主键**。默认取 macOS `LocalHostName`，可用 `CLINE_REPORTER_TERMINAL_ID` 覆盖 |
| `hostname` | string | 是 | 自动探测 | 展示用，默认 macOS `ComputerName` |
| `ide_type` | string | 是 | 目录名映射 | **业务主键**。`vscode` / `vscode-insiders` / `vscodium` / `cursor` / `trae` / `kiro` / `windsurf` / `codebuddy` / `workbuddy` / `qoder`，未知应用取目录名小写 |
| `ide_app` | string | 是 | 目录名 | 原始应用名，如 `Code`、`Trae CN` |
| `source` | string | 是 | 采集来源 | `taskHistory.json`（新版）或 `state.vscdb`（老版） |
| `task_id` | string | 是 | `id` | **业务主键**。Cline 内部任务 id |
| `task_ulid` | string\|null | 否 | `ulid` | Cline 的 ULID，可跨机器追溯 |
| `task_text` | string\|null | 否 | `task` | 任务描述，已被截断至 `task_text_max_len`；`include_task_text=false` 时为 `null` |
| `task_text_hash` | string\|null | 否 | 全文 SHA-1 | 40 位 hex，**基于未截断的全文**计算，可跨机器比对同一任务 |
| `ts` | int\|null | 否 | `ts` | 任务最后更新时间的毫秒戳。**幂等单调守卫依据** |
| `model` | string\|null | 否 | `modelId` | 缺失时回退 `models_used` 末项 |
| `model_provider` | string\|null | 否 | `model_provider_id` | |
| `models_used` | string[] | 是 | `model_usage` | 任务中途使用过的全部模型，按出现顺序去重；可为空数组 |
| `cwd` | string\|null | 否 | `cwdOnTaskInitialization` | 任务启动目录，**可能含敏感路径** |
| `project` | string\|null | 否 | 映射或目录名 | 由 `project_mapping` 子串匹配（最长命中优先），未命中则取 `cwd` basename |
| `tokens_in` | int | 是 | `tokensIn` | 缺失时为 `0` |
| `tokens_out` | int | 是 | `tokensOut` | 缺失时为 `0` |
| `cache_writes` | int | 是 | `cacheWrites` | 缺失时为 `0` |
| `cache_reads` | int | 是 | `cacheReads` | 缺失时为 `0` |
| `total_cost` | float | 是 | `totalCost` | **未配置模型单价时多为 `0`**，计费请以 token 数为准 |
| `size` | int | 是 | `size` | Cline 任务数据字节数 |
| `is_favorited` | bool | 是 | `isFavorited` | |
| `cline_version` | string\|null | 否 | `environment_history` | 取最新一条的 `cline_version` |
| `environment` | object | 是 | `environment_history` | 含 `os_name` / `os_version` / `os_arch` / `host_name` / `host_version`。**整体可能为空对象 `{}`**，即 `task_metadata.json` 缺失；各子字段也可能为 `null` |

> `environment` 为空对象是正常情况（老版本 Cline 无 `task_metadata.json`），不应当作校验失败。

### 3.2 请求体 — 单条模式

`endpoint.mode = "single"` 时，客户端直接发送记录对象，**没有信封**：

```json
{
  "report_type": "cline_usage",
  "schema_version": 1,
  "terminal_id": "your-mac",
  ...
}
```

服务端判定顺序建议：

```
若 body.report_type 以 "_batch" 结尾 → 取 body.records 逐条处理，hostname/terminal_id 用信封值兜底
否则若 body.report_type == "cline_usage" → 视为单条
否则 → 400 invalid_envelope
```

### 3.3 处理语义

```python
def handle_usage_report(body, auth):
    # 1. 鉴权
    require_valid_token(auth)                    # 否则 401

    # 2. 解析信封 —— 只有这一步失败才返回 4xx
    records = extract_records(body)              # 单条模式包装成 [body]
    if records is None:
        return 400, error("invalid_envelope")    # 非 JSON / 无 report_type / records 非数组

    # 3. 逐条落库 —— 字段问题一律宽容处理，绝不因此返回非 2xx
    accepted = 0
    for raw in records:
        try:
            row = coerce(raw)                    # 缺失可选字段补 null，数字字符串转数字
        except UnrecoverableError as e:
            quarantine(raw, str(e))              # 收入隔离表，便于人工排查
            accepted += 1                        # 仍计入 accepted，避免客户端无限重试
            continue
        if not row.get("task_id") or not row.get("terminal_id"):
            quarantine(raw, "missing key fields")
            accepted += 1
            continue
        upsert_usage(row)
        accepted += 1

    # 4. 整批成功
    return 200, {"ok": True, "accepted": accepted, "upserted": accepted}
```

要点：

1. **只有信封解析失败才 4xx。**
2. 单条记录字段异常 → **存入隔离表并返回成功**，用告警暴露，绝不让客户端陷入无限重试。
3. `accepted` 计入被隔离的记录，语义是「本批已处理，无需重发」。

### 3.4 响应

| 状态码 | Body | 场景 |
|---|---|---|
| `200` | `{"ok":true,"accepted":N,"upserted":M}` | 整批已处理。**这是唯一会让客户端标记"已上报"的响应** |
| `400` | `error.code = invalid_envelope` | 请求体非法 JSON、缺 `report_type`、`records` 非数组 |
| `401` | `error.code = unauthorized` | token 缺失/错误（客户端不重试，会进 pending 队列） |
| `403` | `error.code = forbidden` | token 有效但终端未授权 |
| `413` | `error.code = payload_too_large` | 超出体积上限。**见 §8 警告** |
| `415` | `error.code = unsupported_media_type` | `Content-Type` 不符 |
| `429` | `error.code = rate_limited` | 限流。客户端本进程内不重试，该批转入 `pending.jsonl` 等下一轮 |
| `500` / `502` / `503` / `504` | `error.code = internal_error` | 服务端临时故障，客户端会退避重试 |

---

## 4. POST /api/skill/report

Skill 使用上报。结构与 §3 同构，差异如下。

### 4.1 请求体 — 批量模式

```json
{
  "report_type": "cline_skill_batch",
  "schema_version": 1,
  "hostname": "Your Mac",
  "terminal_id": "your-mac",
  "sent_at": 1789306691000,
  "records": [
    {
      "report_type": "cline_skill_usage",
      "schema_version": 1,
      "terminal_id": "your-mac",
      "hostname": "Your Mac",
      "ide_type": "vscode",
      "task_id": "1787244932822",
      "skill": "tac-architecture-html",
      "used_at": 1787244980571,
      "file_count": 3,
      "files": [
        ".cline/skills/tac-architecture-html/references/content-mapping.md",
        ".cline/skills/tac-architecture-html/references/design-system.md",
        ".cline/skills/tac-architecture-html/assets/deck-template.html"
      ],
      "record_sources": ["read_tool"]
    }
  ]
}
```

信封字段与 §3.1 完全一致，仅 `report_type` 换为 `cline_skill_batch`。

**记录字段**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `report_type` | string | 是 | 恒为 `cline_skill_usage` |
| `schema_version` | int | 是 | 当前 `1` |
| `terminal_id` | string | 是 | **业务主键** |
| `hostname` | string | 是 | |
| `ide_type` | string | 是 | **业务主键** |
| `task_id` | string | 是 | **业务主键** |
| `skill` | string | 是 | **业务主键**。Skill 名，取自路径 `.cline/skills/<name>/` 的 `<name>` 段 |
| `used_at` | int\|null | 否 | 毫秒戳。取该 Skill 下**最早**的文件读取时间；无读取时间时回退任务 `ts` |
| `file_count` | int | 是 | `files` 数组长度 |
| `files` | string[] | 是 | 命中的文件路径。可能是相对路径（项目级 `.cline/skills/`）或绝对路径（全局 `~/.cline/skills/`） |
| `record_sources` | string[] | 是 | 文件被记录的方式，取值如 `read_tool`、`user_edited` 等，来自 Cline 的 `record_source`；可为空数组 |

### 4.2 判定语义（供服务端理解数据含义）

客户端判定「任务使用了某个 Skill」的依据是：该任务的 `task_metadata.json` 中 `files_in_context` 出现过匹配 `.cline/skills/<name>/` 的路径。因为 Cline 执行 Skill 必然先读取其 `SKILL.md` 及随附文件，所以这是可靠信号；仅在任务文本里提到 Skill 名而没真实读文件的情况不会误报。

同一任务内的同名 Skill 会**合并为一条**记录，因此：

- `files` / `file_count` 随任务推进**单调增长**
- `used_at` 是**最小值语义**，一旦确定后基本不再变化

### 4.3 处理语义

```python
def upsert_skill(row):
    # 自然键 (terminal_id, ide_type, task_id, skill)
    # files/file_count 可能增长 → 覆盖为较大者
    # used_at 是最小值语义 → 取 MIN(已有, 新值)
    INSERT INTO skill_usage (...) VALUES (...)
    ON CONFLICT (terminal_id, ide_type, task_id, skill_name) DO UPDATE SET
      file_count = GREATEST(skill_usage.file_count, EXCLUDED.file_count),
      files      = EXCLUDED.files,
      used_at    = LEAST(COALESCE(skill_usage.used_at, EXCLUDED.used_at), EXCLUDED.used_at),
      updated_at = now();
```

### 4.4 响应

与 §3.4 完全相同。

---

## 5. GET /api/health

供部署健康检查与配置排障使用。**不需要鉴权**（或允许内网匿名访问）。

```json
{
  "ok": true,
  "service": "cline-usage-reporter-backend",
  "version": "1.0.0",
  "schema_version": 1,
  "server_time": 1789306691000,
  "db": "ok"
}
```

---

## 6. 查询接口

聚合维度刻意与客户端 `stats --by` 对齐，便于「本地看到的数」与「后台看到的数」互相印证。

### 6.1 GET /api/usage/summary

**参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `by` | string | 是 | `model` \| `project` \| `terminal` \| `ide` \| `source` \| `day` \| `task` |
| `from` | string | 否 | `YYYY-MM-DD`，含当日 |
| `to` | string | 否 | `YYYY-MM-DD`，含当日 |
| `terminal_id` | string | 否 | 过滤终端 |
| `project` | string | 否 | 过滤项目 |
| `model` | string | 否 | 过滤模型 |
| `limit` | int | 否 | 默认 100 |

**响应**（指标与客户端 `stats.py` 的 `USAGE_METRICS` 一致）

```json
{
  "ok": true,
  "by": "model",
  "range": { "from": "2026-09-01", "to": "2026-09-13" },
  "totals": {
    "tasks": 27,
    "tokens_in": 2412000,
    "tokens_out": 433300,
    "cache_writes": 688000,
    "cache_reads": 24120000,
    "total_tokens": 2845300,
    "total_cost": 0.2966,
    "size": 12345678
  },
  "rows": [
    {
      "key": "your-model",
      "tasks": 14,
      "tokens_in": 1302000,
      "tokens_out": 221400,
      "cache_writes": 301000,
      "cache_reads": 12004000,
      "total_tokens": 1523400,
      "total_cost": 0.1612,
      "size": 3201004,
      "last_ts": 1789306691000
    }
  ]
}
```

约定：

- `total_tokens = tokens_in + tokens_out`，**不含缓存**（与客户端一致）
- `rows` 按 `total_tokens` 降序
- `cost` 字段名为 `total_cost`，与请求体字段名保持一致
- 未匹配到时 `rows: []`，`totals` 各项为 `0`

### 6.2 GET /api/usage/records

分页明细，字段对齐客户端 `export --format csv` 的列。

| 参数 | 说明 |
|---|---|
| `terminal_id` / `project` / `model` / `ide_type` | 过滤 |
| `from` / `to` | `YYYY-MM-DD` |
| `sort` | `ts`（默认，倒序）\| `total_tokens` \| `total_cost` |
| `page` | 默认 1 |
| `page_size` | 默认 50，上限 500 |

```json
{
  "ok": true,
  "page": 1,
  "page_size": 50,
  "total": 27,
  "records": [
    {
      "terminal_id": "your-mac",
      "hostname": "Your Mac",
      "ide_type": "vscode",
      "ide_app": "Code",
      "task_id": "1787250927116",
      "task_ulid": "01M0G79SGEKY7V2T2PC9V3CP68",
      "task_text": "分析 HR 人才标签管理系统 PRD…",
      "task_text_hash": "7369d5830428956abd822a701d7a1e786e33027a",
      "ts": 1787590155996,
      "model": "your-model",
      "model_provider": "cline",
      "models_used": ["your-model"],
      "cwd": "/Users/you/Desktop/TRAE",
      "project": "TRAE",
      "tokens_in": 1249038,
      "tokens_out": 154866,
      "cache_writes": 343807,
      "cache_reads": 11996800,
      "total_cost": 0.20875371,
      "size": 6960138,
      "is_favorited": false,
      "source": "taskHistory.json",
      "cline_version": "3.76.0"
    }
  ]
}
```

> 若开启了 `include_task_text: false`，`task_text` 为 `null`，此时**禁止**通过其他字段反推原文。

### 6.3 GET /api/skill/ranking

| 参数 | 说明 |
|---|---|
| `from` / `to` | `YYYY-MM-DD` |
| `terminal_id` | 过滤终端 |
| `limit` | 默认 20 |

```json
{
  "ok": true,
  "rows": [
    { "skill": "tac-architecture-html", "tasks": 6, "files": 18, "last_used_at": 1787244980571 }
  ]
}
```

`tasks` 为**去重后的任务数**（`COUNT(DISTINCT task_id)`）。

### 6.4 GET /api/terminals

用于发现「哪些机器在报、哪些机器失联」。

```json
{
  "ok": true,
  "rows": [
    {
      "terminal_id": "your-mac",
      "hostname": "Your Mac",
      "ide_type": "vscode",
      "tasks": 27,
      "last_sent_at": 1789306691000,
      "stale": false
    }
  ]
}
```

`stale` 建议定义为「距 `last_sent_at` 超过 3 个上报周期」（默认周期 30 分钟 → 90 分钟）。这个接口是运维排查「某人数据没上来」的第一入口。

---

## 7. 幂等与并发

### 7.1 为什么必须 upsert

同一个 `task_id` 会被上报**多次**，这是设计使然，不是 bug：

- 会话推进 → token 增长 → 内容指纹变化 → 客户端重报
- `report --force` → 全量重报
- 网络超时后重试 → 同一批可能被提交两次（第一次其实已成功）

因此服务端**绝不能** append 插入，也**绝不能**用「本批已处理过就跳过」这类基于 `(terminal_id, sent_at)` 的粗粒度去重来替代自然键 upsert —— 那会丢掉同一任务后续增长的 token。

### 7.2 单调守卫

用 `ts` 做守卫，保证迟到或乱序的请求不会把新数据覆盖成旧值：

```sql
INSERT INTO usage_record (...) VALUES (...)
ON CONFLICT (terminal_id, ide_type, task_id) DO UPDATE SET
  hostname       = EXCLUDED.hostname,
  task_text      = EXCLUDED.task_text,
  task_text_hash = EXCLUDED.task_text_hash,
  model          = EXCLUDED.model,
  model_provider = EXCLUDED.model_provider,
  models_used    = EXCLUDED.models_used,
  cwd            = EXCLUDED.cwd,
  project        = EXCLUDED.project,
  prompt_tokens  = EXCLUDED.prompt_tokens,
  completion_tokens = EXCLUDED.completion_tokens,
  cache_write_tokens = EXCLUDED.cache_write_tokens,
  cache_read_tokens  = EXCLUDED.cache_read_tokens,
  cost           = EXCLUDED.cost,
  task_size      = EXCLUDED.task_size,
  is_favorited   = EXCLUDED.is_favorited,
  task_ts        = EXCLUDED.task_ts,
  reported_at    = now()
WHERE EXCLUDED.task_ts >= usage_record.task_ts
   OR usage_record.task_ts IS NULL;
```

### 7.3 并发

- 多个终端并发上报不同 `task_id`，天然无冲突。
- 同一终端通常由**单一定时任务**串行上报，不会自我并发；但**手动 `report` 与定时任务可能同时运行**。用数据库层的唯一约束兜底即可，不必加分布式锁。
- 唯一约束冲突应被 `ON CONFLICT` 吸收，不要把 `23505`（PostgreSQL 唯一冲突）暴露为 5xx。

### 7.4 聚合必须基于表

所有统计接口都应直接对 `usage_record` 做聚合。因为 upsert 已保证「一行 = 一个任务的最新状态」，聚合结果天然正确，无需另建汇总表或事件流。

---

## 8. 限流、容量与超时

### 8.1 客户端超时预算

| 项 | 默认值 | 含义 |
|---|---|---|
| `timeout_seconds` | 15 | 单次请求超时 |
| `retries` | 3 | 重试 3 次，共最多 4 次尝试 |
| 退避 | 1s → 2s → 4s | `min(2 ** attempt, 8)` |

单批最坏耗时 ≈ `4 × 15s + 7s ≈ 67s`。建议：

- 服务端 P99 落在 **3s 以内**，留足余量
- 若定时周期设为 30 分钟，67s 不会造成任务堆积；但若设成 **1 分钟**，需确保服务端不会持续超时，否则会并发堆积
- 反向代理（Nginx 等）的 `proxy_read_timeout` 建议 ≥ 30s，低于客户端 15s 超时值即可

### 8.2 请求体体积

单条 usage 记录约 600–900 字节（含中文 `task_text`）；`batch_size` 默认 200 → 单批约 **150–200 KB**。

**警告：客户端失败时不会自动降批。** `batch_size` 是固定配置，一旦服务端返回 413，该批会永远失败并进 pending 队列反复重试，直到有人手动调小 `batch_size`。

因此请把体积上限设得**足够宽松**：

```
建议上限 ≥ 10 MB
```

### 8.3 容量估算

单终端典型量级（参考）：

| 项 | 量级 |
|---|---|
| 任务数 | 数十 ~ 数百 |
| 单批记录数 | ≤ 200 |
| 上报频率 | 每 30 分钟一次 |
| 日增记录 | 首次全量后基本为增量，量级很小 |

Skill 记录远少于 usage 记录（只有真实调用 Skill 的任务才产生）。

### 8.4 限流

`429` 属于 4xx，客户端**不会在本次运行中重试**，该批会转入 `pending.jsonl`，等到下一次定时运行（默认 30 分钟）才重试。

所以：

- 429 是**安全的**，不会丢数据，但会延迟一个周期
- 若服务端长期返回 429，`pending.jsonl` 会持续累积，上报名义上永不完成 —— 应通过监控告警发现
- 建议阈值：**每终端 ≥ 10 次/分钟**，远高于默认 2 次/分钟的实际频率
- 不要依赖 `Retry-After`（客户端不会解析）

---

## 9. 错误码与重试矩阵

| HTTP | `error.code` | 客户端行为 | 数据是否丢失 | 服务端使用建议 |
|---|---|---|---|---|
| `200` | — | 标记整批已上报 | 否 | **唯一的安全成功码** |
| `400` | `invalid_envelope` | 不重试，本批进 pending，下一轮重试 | 否，但会反复重试直到人工修复 | 仅用于信封不可解析。**不要**用于单条记录校验失败 |
| `401` | `unauthorized` | 同上 | 否，但会反复重试 | token 轮换时必须与客户端配置同步更新 |
| `403` | `forbidden` | 同上 | 否 | 终端未授权，应告警让人处理 |
| `404` | `not_found` | 同上 | 否 | 通常是客户端配置的路径写错 |
| `413` | `payload_too_large` | 同上 | 否，但**永远无法成功** | 见 §8.2，务必避免触发 |
| `415` | `unsupported_media_type` | 同上 | 否 | 只在客户端被改坏时出现 |
| `429` | `rate_limited` | 同上（延后一个周期） | 否 | 阈值应远高于实际频率 |
| `500` | `internal_error` | **退避重试** | 否 | 临时故障可放心使用 |
| `502`/`503`/`504` | `upstream_error` | **退避重试** | 否 | 维护窗口可放心使用 |

> **结论：想表达"这条记录有问题"，请返回 200 + 落库/隔离 + 告警，而不是 4xx。** 4xx 在这种客户端模型下等价于「无限重试」，不是「拒绝」。

---

## 10. 安全

| 项 | 建议 |
|---|---|
| 传输 | 强制 HTTPS。HTTP 会明文暴露 token 与任务描述 |
| 鉴权 | 每终端一枚独立 token，便于单独吊销；`Authorization: Bearer <token>` |
| 授权 | token → 允许上报的 `terminal_id` 白名单。不在白名单返回 403 而非 401，便于区分 |
| 体积 | 在反向代理层先做 10 MB 限制，避免超大 body 打到应用层 |
| 输入 | `task_text`、`files`、`cwd` 均视为**不可信文本**。展示时必须转义，禁止直接拼接 SQL |
| 敏感数据 | `cwd` 可能含用户名或内部项目名。建议在服务端用 `project_mapping` 的等价规则做二次收敛后再对外展示，或对查询接口按角色裁剪 `cwd` 字段 |
| 日志 | 不要整批打印 `records`（可能含业务描述）。日志打印 `terminal_id`、批大小、处理结果即可 |
| CORS | 查询接口若提供给 Web 前端，按需配置；上报接口**不需要** CORS |

---

## 11. 参考实现

### 11.1 建表（PostgreSQL）

```sql
CREATE TABLE usage_record (
  id                  BIGSERIAL PRIMARY KEY,
  terminal_id         VARCHAR(64)  NOT NULL,
  hostname            VARCHAR(128),
  ide_type            VARCHAR(32)  NOT NULL,
  ide_app             VARCHAR(64),
  task_id             VARCHAR(64)  NOT NULL,
  task_ulid           VARCHAR(32),
  task_text           TEXT,
  task_text_hash      CHAR(40),
  model               VARCHAR(128),
  model_provider      VARCHAR(64),
  models_used         TEXT[],
  cwd                 TEXT,
  project             VARCHAR(128),
  prompt_tokens       BIGINT       NOT NULL DEFAULT 0,
  completion_tokens   BIGINT       NOT NULL DEFAULT 0,
  cache_write_tokens  BIGINT       NOT NULL DEFAULT 0,
  cache_read_tokens   BIGINT       NOT NULL DEFAULT 0,
  cost                NUMERIC(12,8) NOT NULL DEFAULT 0,
  task_size           BIGINT       NOT NULL DEFAULT 0,
  is_favorited        BOOLEAN      NOT NULL DEFAULT false,
  report_source       VARCHAR(32),
  cline_version       VARCHAR(32),
  os_name             VARCHAR(32),
  os_version          VARCHAR(64),
  os_arch             VARCHAR(32),
  host_name           VARCHAR(64),
  host_version        VARCHAR(64),
  schema_version      SMALLINT     NOT NULL DEFAULT 1,
  task_ts             TIMESTAMPTZ,
  reported_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT uq_usage_natural_key UNIQUE (terminal_id, ide_type, task_id)
);

CREATE INDEX idx_usage_model   ON usage_record (model);
CREATE INDEX idx_usage_project ON usage_record (project);
CREATE INDEX idx_usage_ts      ON usage_record (task_ts DESC);
CREATE INDEX idx_usage_terminal_ts ON usage_record (terminal_id, task_ts DESC);

CREATE TABLE skill_usage (
  id           BIGSERIAL PRIMARY KEY,
  terminal_id  VARCHAR(64)  NOT NULL,
  hostname     VARCHAR(128),
  ide_type     VARCHAR(32)  NOT NULL,
  task_id      VARCHAR(64)  NOT NULL,
  skill_name   VARCHAR(128) NOT NULL,
  used_at      TIMESTAMPTZ,
  file_count   INT          NOT NULL DEFAULT 0,
  files        TEXT[],
  record_sources TEXT[],
  schema_version SMALLINT   NOT NULL DEFAULT 1,
  reported_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT uq_skill_natural_key UNIQUE (terminal_id, ide_type, task_id, skill_name)
);

CREATE INDEX idx_skill_name ON skill_usage (skill_name);
CREATE INDEX idx_skill_used_at ON skill_usage (used_at DESC);

-- 隔离表：字段异常但已"接受"的记录，供人工排查
CREATE TABLE ingest_quarantine (
  id          BIGSERIAL PRIMARY KEY,
  kind        VARCHAR(16),      -- usage | skill
  reason      TEXT,
  raw         JSONB,
  terminal_id VARCHAR(64),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 11.2 路由分派

```python
@app.post("/api/usage/report")
def usage_report(body: dict, auth = Depends(bearer)):
    return _ingest(body, auth, expect="usage")

@app.post("/api/skill/report")
def skill_report(body: dict, auth = Depends(bearer)):
    return _ingest(body, auth, expect="skill")

def _ingest(body: dict, auth, expect: str):
    require_token(auth)                                  # 401

    report_type = body.get("report_type") or ""
    if report_type.endswith("_batch"):                   # 批量信封
        records = body.get("records")
        if not isinstance(records, list) or not records:
            raise ApiError(400, "invalid_envelope", "records must be a non-empty array")
        hostname = body.get("hostname") or ""
        terminal_id = body.get("terminal_id") or ""
    elif report_type:                                    # 单条模式
        records, hostname, terminal_id = [body], body.get("hostname", ""), body.get("terminal_id", "")
    else:
        raise ApiError(400, "invalid_envelope", "missing report_type")

    # 以 report_type 为准分派，而非 URL 路径（skill_path 缺失时会打到这里）
    kind = "skill" if "skill" in report_type else "usage"

    accepted = 0
    for raw in records:
        try:
            row = coerce(raw, kind, hostname, terminal_id)
            if kind == "usage":
                if not row["task_id"] or not row["terminal_id"]:
                    raise UnrecoverableError("missing task_id/terminal_id")
                upsert_usage(row)
            else:
                if not row["skill"] or not row["task_id"]:
                    raise UnrecoverableError("missing skill/task_id")
                upsert_skill(row)
            accepted += 1
        except UnrecoverableError as exc:
            quarantine(kind, str(exc), raw)              # 落隔离表 + 告警
            accepted += 1                                 # 仍计入 accepted，避免客户端无限重试

    return {"ok": True, "accepted": accepted, "upserted": accepted}
```

> 注意 `kind` 由 `report_type` 推导，不依赖 URL —— 这样即使把所有批次都打到同一个端点也能正确入库。

### 11.3 字段归一化要求

`coerce()` 必须做到：

| 输入情况 | 处理 |
|---|---|
| 数字型字段缺失 / 为 `null` | 置 `0`（不要报错） |
| 数字型字段是字符串 `"123"` | 转成 int |
| 布尔字段缺失 | 置 `false` |
| `environment` 缺失或为 `{}` | 各子字段置 `null` |
| `models_used` / `files` / `record_sources` 缺失 | 置空数组 |
| 出现未知字段 | **忽略**，不要因未知字段报错（客户端会随迭代新增字段） |
| `schema_version` 高于已知版本 | 记录告警但仍尝试入库（前向兼容） |

---

## 12. Schema 演进规则

| 变更 | 是否需升 `schema_version` | 服务端动作 |
|---|---|---|
| 新增可选字段 | 否 | 忽略即可 |
| 新增必填字段 | **是** → `2` | 对新版本按其规则校验；对旧版本（`1`）用默认值补齐 |
| 字段语义变更 | **是** | 按 `schema_version` 分支处理，不可混用 |
| 字段重命名 | **是** | 过渡期内两个名字都接受 |

服务端应始终：

1. **接受 `schema_version ≤ 当前支持版本`** 的所有请求
2. 遇到**更高版本**时记告警但尽力入库，而不是直接拒绝 —— 拒绝会造成客户端无限重试
3. 新增字段一律走「可选 + 默认值」，避免版本升级成为破坏性变更

---

## 13. 对客户端 API 的改进建议

以下是当前客户端可以改进、从而简化服务端设计的三点。**均为可选**，不做也不影响现有对接。

### 13.1 在 payload 中带上 `fingerprint`

现状：指纹仅存本地，服务端拿不到。若在 `to_payload()` 中加上 `fingerprint` 字段，服务端可直接用它做唯一键，无需自行复刻 SHA-1 算法。

```python
def to_payload(self) -> Dict[str, Any]:
    data = asdict(self)
    data.pop("record_type", None)
    data["report_type"] = "cline_usage"
    data["fingerprint"] = self.fingerprint()   # 新增
    return data
```

同时可在请求头加 `Idempotency-Key: <batch 内记录的指纹哈希>`，让服务端能识别「同一批的重发」。

### 13.2 支持客户端降批

现状：413 或持续失败时不会自动降批，只能人工改配置。若失败后按 `batch_size / 2` 重试一次，可显著降低 413 造成的永久失败。

### 13.3 区分 4xx 与 5xx 的本地队列策略

现状：无论 4xx 还是 5xx，失败批次都进 `pending.jsonl` 并无条件重试，永久性错误（如 token 失效、413）会反复重试且永远不会成功。

建议：对 `401` / `403` / `413` 这类明确永久性的错误，在队列条目上标记 `permanent` 并控制重试次数上限，避免队列无限增长掩盖问题。

---

## 附：完整调用时序

```
定时任务触发
    │
    ├─ 读取 state.json ── 拿到已上报指纹
    ├─ 读取 pending.jsonl ── 拿到上轮失败记录
    ├─ 采集本地数据 ── 归一化 UsageRecord / SkillRecord
    ├─ 指纹比对 ── 筛出「新增 or 已变化」的记录
    │
    ├─ 按 kind 分组 → usage 批 / skill 批
    │       │
    │       ├─ POST /api/usage/report  (每批 ≤ batch_size)
    │       │      ├─ 200 → 落 state.json（标记已上报）
    │       │      └─ 非 2xx → 落 pending.jsonl（下轮重试）
    │       │
    │       └─ POST /api/skill/report  (同构)
    │
    └─ 写回 state.json / pending.jsonl（原子替换）
```
