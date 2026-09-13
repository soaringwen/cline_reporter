# cline-usage-reporter

> Cline 的 **Token 用量**与 **Skill 使用**本地旁路采集 / 上报工具。

读取 Cline 扩展落在磁盘上的本地数据，聚合后增量上报到你的自建后台。**不修改 Cline、不注入进程、不依赖网关、不需要 VS Code API**，纯读文件即可工作。

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 目录

- [背景](#背景)
- [核心特性](#核心特性)
- [工作原理](#工作原理)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令参考](#命令参考)
- [配置说明](#配置说明)
- [上报数据格式](#上报数据格式)
- [服务端接入](#服务端接入)
- [定时上报](#定时上报)
- [数据源与兼容性](#数据源与兼容性)
- [隐私与安全](#隐私与安全)
- [常见问题](#常见问题)
- [项目结构](#项目结构)
- [开发](#开发)
- [许可证](#许可证)

---

## 背景

企业内网常需要统一统计「谁在用 Cline、用了多少 token、用了哪些模型与 Skill」。常见的几种落地路径中，本工具对应的是**最轻量的一种：读取本地存储旁路上报**。

它的取舍很明确：

| | 说明 |
|---|---|
| 不需要 | 改动 Cline 源码、企业网关、代理拦截、IDE 插件 |
| 只需要 | 一个 Python 解释器，读 Cline 自己写在本地的 JSON |
| 代价 | 只能拿到 Cline **已经持久化**的字段；实时性取决于 Cline 落盘时机 |

实际落地时，Cline 会在 `globalStorage/saoudrizwan.claude-dev/` 下留下两类可用数据：`state/taskHistory.json`（任务级 token 汇总）与 `tasks/<taskId>/task_metadata.json`（模型使用、上下文文件列表等细节）。本工具正是把这两者（以及老版本的 `state.vscdb`）合并成一条条规范化的上报记录。

---

## 核心特性

- **零依赖**：仅用 Python 标准库（`urllib` / `json` / `sqlite3` / `plistlib`），`pip install` 不需要拉任何第三方包。
- **多 IDE 自动发现**：一次扫描所有装了 Cline 扩展的 IDE —— VS Code、VS Code Insiders、VSCodium、Cursor、Trae / Trae CN、Kiro、Windsurf、CodeBuddy / CodeBuddy CN、WorkBuddy、Qoder 等。
- **真正增量上报**：对每条记录计算内容指纹，只有 token / 成本 / 模型等语义字段**发生变化**才重报，避免同一任务随会话推进被反复全量入库。
- **断点续报**：上报失败的记录写入本地 `pending.jsonl`，下次运行自动优先重试，网络抖动不丢数据。
- **版本兼容**：新版 `state/taskHistory.json` 与老版 `state.vscdb` 双通道读取；SQLite 一律 `mode=ro` 只读打开，不影响正在运行的 IDE。
- **Skill 使用识别**：从 `files_in_context` 中匹配 `.cline/skills/<skill名>/` 路径，还原每个任务真实调用过哪些 Skill。
- **本地统计**：不依赖后台即可按模型 / 项目 / 终端 / IDE / 数据源 / 日期 / 任务 / Skill 聚合，支持导出 CSV、JSON。
- **隐私可控**：`include_task_text: false` 时只上报任务描述的 SHA-1，不外泄原文。
- **跨平台定时**：macOS launchd、Linux cron、Windows 计划任务一键安装。

---

## 工作原理

```
       读（只读，不写入 IDE 任何文件）
┌──────────────────────────────────────────────────────┐
│  <App>/User/globalStorage/saoudrizwan.claude-dev/    │
│    ├── state/taskHistory.json      ← 任务级 token     │
│    ├── tasks/<id>/task_metadata.json ← 模型/文件上下文 │
│    └── ../state.vscdb (ItemTable)  ← 老版本兼容        │
└──────────────────────────────────────────────────────┘
                    │
                    ▼  sources.py
                  原始 dict 列表
                    │
                    ▼  collector.py
        归一化 UsageRecord / SkillRecord
        （推导项目名、终端标识、模型归属）
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   stats.py 本地聚合       state.py 增量比对
   （终端表格/CSV/JSON）    （指纹未变则不报）
                                │
                                ▼  reporter.py
                       batch / single HTTP POST
                                │
                    失败 ──▶ pending.jsonl（下次重试）
```

端到端一次运行：

1. `locate.py` 扫描平台对应的应用数据目录，找出所有含 `saoudrizwan.claude-dev` 的 `globalStorage`。
2. `sources.py` 读取任务历史；若新版文件不存在则回退 `state.vscdb`。
3. `collector.py` 逐条归一化，并过滤/映射（排除目录、忽略模型、项目名映射、任务文本脱敏）。
4. `state.py` 用指纹筛出「新增或已变化」的记录，并读入历史失败队列。
5. `reporter.py` 按 `batch_size` 切片 POST，成功则落状态，失败则入队。

---

## 安装

要求 **Python ≥ 3.9**，无第三方依赖。

### 方式一：直接运行（免安装）

```bash
git clone <your-repo-url> cline-usage-reporter
cd cline-usage-reporter
python3 -m cline_reporter doctor
```

### 方式二：安装为命令（推荐）

```bash
pip install -e .
# 或
pipx install .
```

安装后可直接使用 `cline-reporter`：

```bash
cline-reporter doctor
```

> 开发模式安装（`-e`）在修改代码后无需重装；定时任务会以 `python -m cline_reporter` 方式调用，路径稳定。

---

## 快速开始

```bash
# 1) 环境自检：看看探测到了哪些 IDE、各数据源是否存在、能采集到多少条
python3 -m cline_reporter doctor

# 2) 本地统计（不需要配置任何后台地址）
python3 -m cline_reporter stats --skill

# 3) 空跑上报：只打印将要发送的内容，不发起真实请求
python3 -m cline_reporter report --dry-run

# 4) 配置后台地址后正式上报
cp config.example.json ~/.cline-usage-reporter/config.json
${EDITOR:-vim} ~/.cline-usage-reporter/config.json   # 填 endpoint.url 与 token
python3 -m cline_reporter report

# 5) 安装定时任务：每 30 分钟采集上报一次
python3 -m cline_reporter schedule install --interval 30
```

`doctor` 的典型输出：

```
cline-usage-reporter v1.0.0 环境自检
================================================================
Python            : 3.12.4 (/usr/bin/python3)
配置文件          : /Users/you/.cline-usage-reporter/config.json
状态目录          : /Users/you/.cline-usage-reporter
终端标识          : terminal_id=your-mac  hostname=Your Mac
usage 上报地址    : https://your-backend.com/api/usage/report
skill 上报地址    : https://your-backend.com/api/skill/report
上报模式          : batch / batch_size=200
包含任务文本      : True

发现 Cline 存储 1 处：
  - Code (vscode) -> /Users/you/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev
      taskHistory.json: ✓ | tasks/ 目录: 27 个 | task_metadata.json: 27 个 | state.vscdb: ✓

taskHistory 记录总数: 27

执行一次试采集…
  归一化 usage 记录 : 27
  归一化 skill 记录 : 6
  命中的 skill      : tac-architecture-html
```

`stats` 的典型输出：

```
终端: Your Mac (your-mac)
任务数: 27   总 token: 2,845,300
  输入 2,412,000 | 输出 433,300 | 缓存读 24,120,000 | 缓存写 688,000 | 费用 0.296600

按模型统计（token 用量）
模型                  任务数  输入 token  输出 token  缓存写    缓存读     合计 token  费用      数据量(B)
--------------------  ------  ----------  ----------  --------  ---------  ----------  --------  ---------
your-model-a          14      1,302,000   221,400     301,000   12,004,000 1,523,400   0.161200  3,201,004
your-model-b          9       812,000     152,900     240,000   8,120,000  964,900     0.094100  2,110,220
...
```

---

## 命令参考

所有子命令都支持 `--config <路径>` 指定配置文件。

| 命令 | 说明 |
|---|---|
| `doctor` | 环境自检。打印生效配置、终端标识、发现的存储路径、各数据文件状态、归一化后的记录数与命中的 Skill 名。 |
| `collect [--json] [--out FILE]` | 采集并预览。`--json` 输出全量明细；`--out` 按扩展名导出 `.json` 或 `.csv`。 |
| `stats [--by DIM]... [--no-skill] [--json]` | 本地统计。`--by` 可重复，取值 `model` `project` `terminal` `ide` `source` `day` `task`，默认 `model,project,terminal,ide,day`；Skill 统计默认开启，用 `--no-skill` 关闭。 |
| `report [--dry-run] [--force] [--limit N] [--no-pending]` | 增量上报。`--dry-run` 只预览；`--force` 忽略增量状态全量重报；`--limit` 限制本次条数；`--no-pending` 不重试历史失败队列。 |
| `export --format csv\|json [--out FILE] [--no-skill]` | 导出明细。不指定 `--out` 则打印到标准输出。 |
| `schedule install\|uninstall\|status [--interval 分钟] [--dry-run]` | 定时任务管理，`--interval` 默认 30 分钟。 |

退出码：`0` 成功、`1` 部分或全部上报失败、`2` 用法/配置错误（如未配置上报地址）。

常用示例：

```bash
# 只看某一维度
python3 -m cline_reporter stats --by model --by day --no-skill

# 导出全量明细，方便用 Excel / BI 分析
python3 -m cline_reporter export --format csv --out ~/cline-usage.csv

# 统计结果直接喂给 jq
python3 -m cline_reporter stats --json | jq '.model[0]'

# 全量重报（例如后台库被清空后重建）
python3 -m cline_reporter report --force
```

---

## 配置说明

配置文件为 JSON，**先命中先用**，搜索顺序：

1. `--config <路径>` 显式指定（不存在则直接报错，避免静默跑错端点）
2. 环境变量 `CLINE_REPORTER_CONFIG` 指向的路径
3. `./cline-usage-reporter.config.json`
4. `./config.json`
5. `~/.cline-usage-reporter/config.json`

> **定时任务场景请把配置放在 `~/.cline-usage-reporter/config.json`。** launchd 运行时的 `WorkingDirectory` 是 `$HOME`，第 3、4 项在定时执行时会解析到 `$HOME` 下。安装定时任务时若已找到配置文件，其绝对路径会被写进任务定义，因此之后修改配置内容无需重装，但**配置搬家或改名后必须重装**。

完整示例见 [`config.example.json`](config.example.json)：

```jsonc
{
  "endpoint": {
    "url": "https://your-backend.com",     // 后台根地址
    "usage_path": "/api/usage/report",     // token 用量路径（也可直接填完整 URL）
    "skill_path": "/api/skill/report",     // skill 路径；留空则回退 usage_path
    "token": "",                           // 非空则加 Authorization: Bearer <token>
    "headers": {},                         // 额外请求头，如 {"X-Env": "prod"}
    "timeout_seconds": 15,
    "retries": 3,                          // 4xx 不重试，网络错误指数退避重试
    "mode": "batch",                       // batch=按批提交；single=一条一请求
    "batch_size": 200                      // 单批最大记录数
  },
  "collect": {
    "ide_roots": [],                       // 自动发现失败时手动补路径
    "include_task_text": true,             // false=只上报 hash，不外泄任务原文
    "task_text_max_len": 200,              // 任务文本截断长度（hash 始终基于全文）
    "include_skill_records": true,
    "include_legacy_vscdb": true,
    "project_mapping": {                   // cwd 子串 -> 项目名，最长匹配优先
      "/Users/you/CodeBuddy/": "CodeBuddy",
      "/Users/you/Desktop/TRAE": "TRAE"
    },
    "exclude_cwd_prefixes": [],            // 跳过这些 cwd 前缀下的任务
    "ignored_skill_names": [],             // 忽略的 skill 名（大小写不敏感）
    "ignored_models": []                   // 忽略的模型（子串匹配）
  },
  "terminal": {
    "id": "",                              // 留空自动探测
    "name": ""
  },
  "state": {
    "dir": "~/.cline-usage-reporter"       // 增量状态、待重试队列、日志
  }
}
```

环境变量：

| 变量 | 作用 |
|---|---|
| `CLINE_REPORTER_CONFIG` | 指定配置文件路径 |
| `CLINE_REPORTER_TERMINAL_ID` | 覆盖终端稳定标识 |
| `CLINE_REPORTER_TERMINAL_NAME` | 覆盖终端显示名 |

终端标识推导顺序：环境变量 → macOS `scutil --get LocalHostName` / `ComputerName` → `platform.node()`。

---

## 上报数据格式

`mode=batch` 时按批提交；`mode=single` 时逐条提交单条记录对象（去掉外层信封）。

### usage（Token 用量）

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
        "os_name": "darwin", "os_version": "25.4.0", "os_arch": "arm64",
        "host_name": "Visual Studio Code", "host_version": "1.98.2"
      }
    }
  ]
}
```

字段说明：

- 命名统一为后端友好的 `snake_case`，与 Cline 原始字段对应关系：
  `tokensIn→tokens_in`、`tokensOut→tokens_out`、`cacheWrites→cache_writes`、`cacheReads→cache_reads`、`totalCost→total_cost`、`modelId→model`、`cwdOnTaskInitialization→cwd`、`ulid→task_ulid`。
- `ts` 为**毫秒**时间戳，取 Cline 记录的最近更新时间。
- `model` 优先取 `modelId`；缺失时用 `task_metadata.model_usage` 的最后一个模型回填。
- `models_used` 来自 `task_metadata.json` 的 `model_usage`，可反映一个任务中途切换过哪些模型。
- `total_cost` 原样透传（未配置模型单价时 Cline 往往给 0），计费建议以 token 数为准。
- 未知字段一律不猜测，缺失即 `null` / `0`。

### skill（Skill 使用）

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

**判定逻辑**：任务读取过 `.cline/skills/<skill名>/` 下的任意文件即视为使用了该 Skill（Cline 执行 Skill 必然先读其 `SKILL.md` 及随附文件）。同一任务内同名 Skill 的多个文件会合并为一条记录，`used_at` 取首次读取时间。仅在任务文本里**提到**某 Skill 但未真实读取文件的，不会误报。项目级 `.cline/skills/` 与全局 `~/.cline/skills/` 均可命中。

---

## 服务端接入

服务端只需提供两个接受 `POST application/json` 的接口，返回 2xx 即视为成功。

> 完整的接口契约见 **[API.md](API.md)** —— 含逐字段规格、错误码与重试矩阵、幂等 SQL、参考实现与容量估算。本节只给最小必要信息。

### 幂等处理（重要）

同一条任务的 token 会**随会话推进持续增长**，因此同一个 `task_id` 会被多次上报。客户端已通过内容指纹变化来触发重报，服务端也必须做幂等 upsert，否则会产生重复行。

**推荐**直接用自然键 `(terminal_id, ide_type, task_id)` 做主键 upsert，并用 `ts` 做单调守卫（`WHERE EXCLUDED.task_ts >= 已有.task_ts`），避免乱序请求把新值覆盖成旧值。客户端内部增量键为 `usage|<ide_type>|<task_id>`、`skill|<ide_type>|<task_id>|<skill>`，可用于排查。

> 注意：客户端**不上报** `fingerprint` 字段，指纹只存在本地 `state.json` / `pending.jsonl` 中。若想用指纹做唯一键，服务端需自行复刻客户端的 SHA-1 算法，其中涉及 Python `float` 的序列化细节，跨语言复现容易不一致 —— 因此不建议。

### 批次语义

客户端**只以 HTTP 状态码判断整批成败，且会忽略成功响应体**。因此服务端必须「整批全收或整批拒收」：返回 200 却在 body 里报告部分记录被拒，客户端会把整批标记为已上报，被拒的记录将**永久丢失**。单条记录字段异常时应返回 200 并落库或写入隔离表，而不是返回 4xx。

### 建表参考（PostgreSQL）

> 下面是精简可用的版本。完整版（含隔离表、更多索引、幂等 upsert SQL、并发与容量处理）见 [API.md § 参考实现](API.md#11-参考实现)。

```sql
-- Token 用量
CREATE TABLE usage_record (
  id                  BIGSERIAL PRIMARY KEY,
  terminal_id         VARCHAR(64),      -- 终端标识
  hostname            VARCHAR(128),     -- 机器名
  ide_type            VARCHAR(32),      -- vscode / cursor / trae / codebuddy ...
  ide_app             VARCHAR(64),
  task_id             VARCHAR(64),
  task_ulid           VARCHAR(32),
  task_text           TEXT,
  task_text_hash      CHAR(40),
  model               VARCHAR(128),
  model_provider      VARCHAR(64),
  cwd                 TEXT,
  project             VARCHAR(128),
  prompt_tokens       BIGINT,
  completion_tokens   BIGINT,
  cache_write_tokens  BIGINT,
  cache_read_tokens   BIGINT,
  cost                NUMERIC(12,8),
  task_size           BIGINT,
  is_favorited        BOOLEAN,
  report_source       VARCHAR(32),      -- taskHistory.json / state.vscdb
  cline_version       VARCHAR(32),
  reported_at         TIMESTAMPTZ DEFAULT now(),
  task_ts             TIMESTAMPTZ,      -- Cline 记录的最后更新时间（幂等单调守卫）
  UNIQUE (terminal_id, ide_type, task_id)
);

CREATE INDEX idx_usage_model   ON usage_record (model);
CREATE INDEX idx_usage_project ON usage_record (project);
CREATE INDEX idx_usage_ts      ON usage_record (task_ts DESC);

-- Skill 使用
CREATE TABLE skill_usage (
  id          BIGSERIAL PRIMARY KEY,
  terminal_id VARCHAR(64),
  hostname    VARCHAR(128),
  ide_type    VARCHAR(32),
  task_id     VARCHAR(64),
  skill_name  VARCHAR(128),
  used_at     TIMESTAMPTZ,
  file_count  INT,
  UNIQUE (terminal_id, ide_type, task_id, skill_name)
);
```

常用统计查询：

```sql
-- 按模型汇总
SELECT model, COUNT(*) AS tasks,
       SUM(prompt_tokens + completion_tokens) AS total_tokens,
       SUM(cost) AS total_cost
FROM usage_record GROUP BY model ORDER BY total_tokens DESC;

-- 按项目汇总
SELECT project, COUNT(*) AS tasks, SUM(cache_read_tokens) AS cache_reads
FROM usage_record GROUP BY project ORDER BY tasks DESC;

-- 按终端汇总
SELECT terminal_id, hostname, COUNT(*) AS tasks, SUM(cost) AS total_cost
FROM usage_record GROUP BY terminal_id, hostname;

-- Skill 使用排行
SELECT skill_name, COUNT(DISTINCT task_id) AS task_count
FROM skill_usage GROUP BY skill_name ORDER BY task_count DESC;
```

---

## 定时上报

```bash
python3 -m cline_reporter schedule install --interval 30   # 每 30 分钟
python3 -m cline_reporter schedule install --dry-run       # 先看将生成的配置
python3 -m cline_reporter schedule status
python3 -m cline_reporter schedule uninstall
```

| 平台 | 实现 | 位置 / 日志 |
|---|---|---|
| macOS | `launchd` LaunchAgent（`StartInterval` + `RunAtLoad`） | `~/Library/LaunchAgents/com.cline-usage-reporter.plist`；日志在 `<state.dir>/logs/` |
| Linux | 追加 crontab 条目（带 `# com.cline-usage-reporter` 注释便于卸载） | 由 `crontab -l` 查看 |
| Windows | 打印 `schtasks` 命令，需在管理员 PowerShell 中执行 | 由任务计划程序查看 |

排查定时任务：

```bash
# 查看实际写入的任务定义（确认引用的 config 路径）
plutil -p ~/Library/LaunchAgents/com.cline-usage-reporter.plist

# 查看运行日志
tail -f ~/.cline-usage-reporter/logs/reporter.out.log
tail -f ~/.cline-usage-reporter/logs/reporter.err.log
```

---

## 数据源与兼容性

自动发现位置（每个 IDE 一个目录）：

| 平台 | 扫描根目录 |
|---|---|
| macOS | `~/Library/Application Support/*/User/globalStorage/saoudrizwan.claude-dev` |
| Linux | `~/.config/*/User/globalStorage/saoudrizwan.claude-dev`、`~/.var/app/*/...` |
| Windows | `%APPDATA%\*\User\globalStorage\saoudrizwan.claude-dev` |

支持识别的 IDE（`ide_type`）：`vscode`、`vscode-insiders`、`vscodium`、`cursor`、`trae`、`kiro`、`windsurf`、`codebuddy`、`workbuddy`、`qoder`；未收录的应用会以目录名小写作为 `ide_type` 兜底，不会漏采。

每个存储按顺序尝试：

1. `state/taskHistory.json` —— 新版，任务进行中即写入，实时性最好。兼容 `[{...}]` 与 `{"taskHistory": [...]}` 两种结构。
2. `globalStorage/state.vscdb` → `ItemTable` 键 `saoudrizwan.claude-dev` 中的 `taskHistory` —— 老版本。
3. `tasks/<taskId>/task_metadata.json` —— 可选增强，缺失时该任务仍会上报 usage，只是 `models_used` / Skill 为空。

容错约定：任一存储读取失败只告警不中断，其余存储继续处理；`collect.ide_roots` 可手动补充扫描路径（支持传 `globalStorage`、`User` 或应用目录三种层级）。

---

## 隐私与安全

- **只读**：从不写入 Cline 的任何文件；SQLite 以 `mode=ro` 打开，避免锁库。
- **任务文本**：`include_task_text: false` 时 `task_text` 置空，仅上报 `task_text_hash`（全文 SHA-1），后台仍可去重与关联。
- **本地状态**：所有增量状态、待重试队列、日志都限制在 `state.dir`（默认 `~/.cline-usage-reporter/`），可直接删除以重置。
- **鉴权**：`endpoint.token` 使用 `Authorization: Bearer` 头；如需自定义鉴权，用 `endpoint.headers` 覆盖。
- 建议在企业场景中将 `cwd` 视为敏感信息，通过 `project_mapping` 把真实路径收敛成业务项目名后再上报。

---

## 常见问题

**Q：`doctor` 显示「未发现任何 Cline 存储」怎么办？**
确认已安装 Cline 扩展且至少启动过一次。若安装在非常规位置（如自定义 user-data-dir），用 `collect.ide_roots` 指定：

```json
{ "collect": { "ide_roots": ["/custom/path/Code/User/globalStorage"] } }
```

**Q：`report` 报「未配置上报地址」并以 2 退出。**
说明 `endpoint.url` 为空，且 `usage_path` 也不是完整 URL。填入 `url`，或先用 `--dry-run` 预览（`--dry-run` 不需要地址）。

**Q：每次运行都重报同样的数据？**
该任务的 token 仍在增长（指纹随之变化），属预期行为，服务端需做幂等 upsert。若指纹未变却仍被重报，检查 `state.json` 是否已被删除或 `--force` 是否被误用。

**Q：上报失败后数据会丢吗？**
不会。失败记录写入 `<state.dir>/pending.jsonl`，下次运行自动优先重试；成功后会重写该文件。用 `--no-pending` 可临时跳过重试。

**Q：改了配置但定时任务没生效？**
修改配置**内容**无需动作；若配置文件**路径**变了，必须重新 `schedule install` —— 旧任务定义里存的是原绝对路径。

**Q：`total_cost` 全是 0？**
Cline 在未配置模型单价时确实记为 0。本工具原样上报，计费请以 `tokens_in` / `tokens_out` / `cache_reads` / `cache_writes` 为准。

**Q：能采到更多字段吗？**
`conversationHistoryDeletedRange` 等可选字段当前未采集（不影响统计）。需要时在 `sources.py` / `collector.py` 中扩展即可。

---

## 项目结构

```
cline_reporter/
├── __init__.py     # 版本号与 SCHEMA_VERSION
├── __main__.py     # python -m cline_reporter 入口
├── cli.py          # 子命令解析与编排
├── config.py       # 配置加载、深合并、上报地址拼接
├── locate.py       # 跨平台发现 Cline 存储（IDE 类型映射）
├── sources.py      # taskHistory.json / task_metadata.json / state.vscdb 读取
├── collector.py    # 归一化为 UsageRecord / SkillRecord
├── state.py        # 增量指纹 + 失败重试队列（原子写入）
├── reporter.py     # HTTP 上报（批量/单条、重试、鉴权、dry-run）
├── stats.py        # 聚合、终端表格渲染、CSV/JSON 导出
└── scheduler.py    # launchd / cron / schtasks 定时任务
config.example.json # 配置模板
pyproject.toml      # 打包元数据与 cline-reporter 命令入口
API.md              # 后台接口规格（服务端对接用）
```

运行时状态文件：

| 文件 | 说明 |
|---|---|
| `<state.dir>/state.json` | 已上报记录的指纹（超 20000 条按时间淘汰最旧） |
| `<state.dir>/pending.jsonl` | 上报失败待重试队列，每行一条记录 |
| `<state.dir>/logs/` | 定时任务 stdout / stderr 日志 |

---

## 开发

```bash
git clone <your-repo-url> && cd cline-usage-reporter
pip install -e .

# 直接用源码运行，无需安装
python3 -m cline_reporter doctor

# 语法/导入自检
python3 -m compileall cline_reporter
```

代码约定：单文件单职责，模块间只通过 dataclass 传递数据；采集层不做 I/O 副作用；所有外部输入（磁盘 JSON、HTTP 响应）都必须容错，绝不因单条脏数据中断整批。

---

## 许可证

MIT
