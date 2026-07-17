# 私有真实项目受控试运行与数据驱动维护手册

状态：**项目准备期；真实 Workspace Gate F 尚未启用**

适用对象：私有科研项目、项目所有者与 Codex 协作

首轮观察周期：Gate F 和启动门槛通过后的连续 4 周

详细安全门禁：[真实科研项目 Pilot 逐步实施手册](REAL_PROJECT_PILOT.zh-CN.md)

## 1. 目的和当前结论

本手册把 KnowledgeHub 从冻结发布状态带入“受控试运行 + 数据驱动维护”阶段。目标不是立即扩建或替换正式索引，而是：

1. 安全创建并准入一个私有真实科研项目；
2. 证明 KnowledgeHub 在真实任务中持续可用、可追踪且不修改项目；
3. 用脱敏运行数据识别重复问题，而不是凭单次体验修改系统；
4. 只在回归证据充分时提出候选发布建议；
5. 保持正式 collection、alias、数据库、原始文档和用户项目可恢复。

当前代码仍是 Fixture-only：`knowledgehub workspace create --help` 没有
`--allow-real-project`，项目 Query/Skill 仍不能安全接入真实 Workspace。因此，真实项目可以先创建，现有三库也可以先做只读基线检查，但四周 Pilot **不得开始计时**，且不得执行 Gate G～J，直到第 6 节 Gate F 完成。

## 2. 命令分级和职责

本手册使用以下标记：

- **[R] 只读**：只读取代码、状态或服务，可直接执行；
- **[W-REPORT] 报告写入**：只写批准的私有报告目录；
- **[W-STATE] 状态写入**：写 KnowledgeHub 独立状态或明确反馈，执行前由项目所有者确认；
- **[H] 高风险**：可能构建、发布、回滚、清理或改变服务；首轮只记录建议，不执行。
- **[TARGET] 目标接口**：Gate F 交付后才存在；当前禁止执行。

### 2.1 项目所有者负责

- 创建私有项目，决定数据、源码和日志的访问边界；
- 维护密钥和外部环境文件；
- 对检索结果给出主观评价和 Writing feedback；
- 批准所有状态写入、服务变更、candidate、promote、rollback 和 cleanup；
- 在 Pilot 会话之外正常开发项目，并形成独立、可恢复的 Git 提交。

### 2.2 Codex 负责

- 只读检查、命令预览、静态 Repository Intake 和脱敏环境分析；
- 整理基线、每日记录、问题聚类和周报；
- 提出改进方案和回归计划；
- 仅在明确批准后进行有限写入或验证；
- 不替项目创建首个提交，不自动 stash、reset、clean、安装依赖或运行训练。

### 2.3 Pilot 会话不变性

每次 Pilot 会话开始和结束都记录真实项目的 `HEAD` 与工作区状态。会话期间暂停人工开发；如项目所有者必须修改项目，应结束当前会话，提交或记录用户自有改动，再开始新会话。

KnowledgeHub 或 Codex 导致项目文件、`HEAD` 或 dirty 状态发生变化时，本次会话立即判定为 **FAIL**。不要自动 reset 或覆盖用户改动。

## 3. 创建私有真实项目

本节由项目所有者执行。KnowledgeHub 只在项目已有首个可恢复提交后接入。

### 3.1 目录边界

真实项目必须满足：

- 不位于 `/home/lengmo/KnowledgeHub` 内；
- 不位于 `/data/KnowledgeHub` 正式知识库目录内；
- 不是 `state/fixtures` 的子目录；
- 项目仓库、数据集、模型、状态和 Pilot 报告使用不同目录；
- KnowledgeHub 账户只获得完成静态读取所需的最小权限。

先确定占位值，但不要把密钥写入这些变量：

```bash
export KH_REPO=/home/lengmo/KnowledgeHub
export KH_BIN=/home/lengmo/anaconda3/envs/rag/bin/knowledgehub
export KH_PILOT_REPO=/absolute/path/to/private-research-project
export KH_PILOT_ID=my-private-project
export KH_PILOT_STATE=/data/KnowledgeHub/projects/$KH_PILOT_ID
export KH_CYCLE_ID=pilot-$(date +%Y%m%d)
export KH_PILOT_REPORTS=/data/KnowledgeHub/reports/controlled-pilot/$KH_CYCLE_ID
```

**[R]** 检查占位值和隔离关系：

```bash
test -x "$KH_BIN"
test -d "$KH_PILOT_REPO"
test "$KH_PILOT_REPO" != "$KH_REPO"
printf '%s\n' "$KH_PILOT_ID" | rg '^[a-z0-9][a-z0-9._-]{2,79}$'
printf 'repo=%s\nproject=%s\nstate=%s\nreports=%s\n' \
  "$KH_REPO" "$KH_PILOT_REPO" "$KH_PILOT_STATE" "$KH_PILOT_REPORTS"
```

输出中仍有 `/absolute/path/`、路径互相嵌套或 ID 校验失败时立即停止。

### 3.2 项目 Git 和敏感数据

项目所有者应自行完成：

- 项目级 `.gitignore`；
- README、依赖声明和最小代码骨架；
- 数据集、checkpoint、模型、`.env`、密钥、原始日志和临时输出的外置或忽略；
- 对准备提交的文件逐项审查；
- 首个可恢复 Git commit。

不要使用未经审查的 `git add -A`。KnowledgeHub 和 Codex 不替项目初始化或提交。

**[R]** 首个提交完成后检查：

```bash
git -C "$KH_PILOT_REPO" rev-parse --show-toplevel
git -C "$KH_PILOT_REPO" rev-parse --verify HEAD
git -C "$KH_PILOT_REPO" branch --show-current
git -C "$KH_PILOT_REPO" status --short
git -C "$KH_PILOT_REPO" ls-files | \
  rg -i '(^|/)(\.env|secrets?|credentials?|private[-_]?key)(\.|/|$)' || true
```

最后一条命令只检查受跟踪文件名，不读取或打印秘密内容。出现可疑路径时由项目所有者人工确认，未确认前不得继续。

## 4. 私有状态、报告目录和记录原则

### 4.1 建立目录

**[W-REPORT]** 经项目所有者确认路径后创建私有目录：

```bash
umask 077
install -d -m 0700 \
  "$KH_PILOT_STATE" \
  "$KH_PILOT_REPORTS/baseline" \
  "$KH_PILOT_REPORTS/daily" \
  "$KH_PILOT_REPORTS/queries" \
  "$KH_PILOT_REPORTS/issues" \
  "$KH_PILOT_REPORTS/weekly"
```

检查路径和权限：

```bash
stat -c '%a %U:%G %n' "$KH_PILOT_STATE" "$KH_PILOT_REPORTS"
find "$KH_PILOT_REPORTS" -maxdepth 1 -type d -printf '%m %u:%g %p\n'
```

目录应为 `700`。报告文件应为 `600`；不要用 `sudo` 绕过归属或权限错误。

### 4.2 允许记录的内容

- 相对路径、Git commit、配置哈希和证据 ID；
- 脱敏的依赖名、版本、运行结果和查询摘要；
- collection/alias 名、计数、延迟和错误类别；
- 有界、脱敏的日志摘要。

### 4.3 禁止记录的内容

- bearer token、API key、密码、Cookie、完整环境变量；
- 私人论文全文、原始数据样本、未公开源码正文；
- 完整用户提示或可能反推出私有研究内容的长查询；
- 未经审查的 stdout/stderr、traceback 或训练日志；
- 真实项目的绝对路径清单。报告中优先使用项目内相对路径。

## 5. Day 0：KnowledgeHub 基线

四周计时前先建立冻结基线。所有命令从 `$KH_REPO` 执行。

### 5.1 代码、发布和完整性

**[R]** 执行：

```bash
cd "$KH_REPO"
git rev-parse HEAD
git status --short
"$KH_BIN" release validate
"$KH_BIN" validate all
"$KH_BIN" validate dependencies --offline
"$KH_BIN" mcp validate
"$KH_BIN" mcp tools
"$KH_BIN" index alias-status code
"$KH_BIN" index alias-status writing
"$KH_BIN" writing-v2 feedback-status
```

要求：

- release、integrity、dependencies 和 MCP schema 均成功；
- `validate all` 完成在线 Qdrant membership 检查；
- 仅运行 `validate all --offline` 时必须标记 `qdrant_not_checked`，不能判定完整上线基线通过；
- Code/Writing alias 与冻结发布记录一致；
- KnowledgeHub 工作区没有未知改动。

### 5.2 服务和 readiness

**[R]** 执行：

```bash
systemctl is-active knowledgehub-rag-core.service
systemctl is-active knowledgehub-rag-search-api.service
systemctl is-active knowledgehub-mcp-lan.service
systemctl is-active knowledgehub-mcp-tailscale.service
curl -fsS http://10.249.44.27:8091/healthz
curl -fsS http://10.249.44.27:8091/readyz
curl -fsS http://127.0.0.1:8092/healthz
curl -fsS http://127.0.0.1:8092/readyz
```

`healthz` 只代表进程存活；任一 `readyz` 为 degraded/failed 时不能开始 Pilot。Search API 的鉴权健康检查按[双 3090 构建指南](BUILD_ZOTERO_RAG_DUAL_3090.zh-CN.md)执行，不把 token 写入命令记录或报告。

### 5.3 Evaluation 基线

**[W-REPORT]** 写入私有报告目录：

```bash
"$KH_BIN" evaluate run --mode offline --profile v2 \
  --output "$KH_PILOT_REPORTS/baseline/eval-offline-v2.json"
"$KH_BIN" evaluate run --mode live --profile v2 \
  --output "$KH_PILOT_REPORTS/baseline/eval-live-v2.json"
```

保存退出码和报告哈希，不把报告提交到 Git：

```bash
sha256sum "$KH_PILOT_REPORTS/baseline/"*.json
```

## 6. Gate F：启用真实项目支持

### 6.1 当前必须停止的位置

**[R]** 检查目标参数：

```bash
"$KH_BIN" workspace create --help | rg -- '--allow-real-project'
```

当前版本预期无输出并返回非零；这表示 Gate F 尚未实现。此时不得尝试创建真实 Workspace，也不得对真实项目执行 `project context/query/skill`。

### 6.2 交给 Codex 的实施任务

真实项目已有首个提交后，将下面整段交给 Codex，并替换三个占位值：

```text
基于 docs/guides/REAL_PROJECT_PILOT.zh-CN.md 和
docs/guides/CONTROLLED_PILOT_DATA_DRIVEN_MAINTENANCE.zh-CN.md，
为一个私有真实科研项目实现 V3 只读 Pilot Gate F。

目标仓库：<KH_PILOT_REPO>
Workspace ID：<KH_PILOT_ID>
独立状态根：<KH_PILOT_STATE>

要求：
1. 先分析并给出计划，不运行或修改目标项目代码；
2. 不安装目标项目依赖，不训练，不下载模型，不写正式向量索引；
3. 增加显式 --allow-real-project，默认仍拒绝真实 Workspace；
4. 真实项目仅允许 workspace_type=project、data_scope=private 或 project；
5. 状态根不得位于 state/fixtures、真实仓库或正式知识库目录；
6. 使用通用只读 Router 和 Workspace scope，不复用 FixtureKnowledgeRouter；
7. cleanup 对真实 Workspace 永远拒绝，archive 只能修改独立状态根；
8. MCP 对状态根只读，未知 Workspace 和路径穿越 fail-closed；
9. 测试必须证明不会 import、执行或安装目标项目；
10. 增加 project/fixture 隔离、路径、权限、MCP 和回归测试；
11. 提供与 CLI Schema 一致的私有项目 Workspace 示例配置；
12. 实施后运行 pytest、Ruff、strict MyPy、diff check，并更新相关文档。
```

### 6.3 Gate F 验收

只有以下条件全部满足，四周计时才可以开始：

- `workspace create --help` 显示 `--allow-real-project`；
- 没有 opt-in 时真实 Workspace 仍被拒绝；
- Fixture 现有测试全部通过；
- 新测试覆盖路径穿越、独立状态根、真实 cleanup 拒绝和 MCP 只读；
- Query/Skill 不再为真实 Workspace 实例化 Fixture Router；
- 目标项目未被修改，也未执行任何目标代码；
- pytest、Ruff、strict MyPy 和 `git diff --check` 全部通过；
- [真实项目 Pilot 手册](REAL_PROJECT_PILOT.zh-CN.md)已同步为实际接口。

Gate F 通过后，从详细手册 Gate G 创建和验证 Workspace。Gate G～J 命令属于 **[TARGET]**，在接口实际出现前不得从任何文档复制执行。

## 7. 四周受控试运行

四周从 Gate F、真实 Workspace、Day 0 基线和 readiness 全部通过后的下一个完整工作日开始。

### 7.1 第 1 周：基线与只读接入

目标：证明真实项目能被静态理解，且三库、Workspace、MCP 都不会修改项目。

1. **[R]** 每次会话前记录项目状态：

   ```bash
   git -C "$KH_PILOT_REPO" rev-parse HEAD
   git -C "$KH_PILOT_REPO" status --short
   ```

2. **[W-REPORT]** 先 dry-run 捕获脱敏环境：

   ```bash
   "$KH_BIN" environment capture \
     --name "$KH_PILOT_ID" \
     --project "$KH_PILOT_REPO" \
     --dry-run \
     > "$KH_PILOT_REPORTS/baseline/environment-capture-dry-run.json"
   ```

3. 人工检查输出只含依赖声明路径、哈希和脱敏环境。经批准后，**[W-STATE]** 保存正式环境快照：

   ```bash
   "$KH_BIN" environment capture \
     --name "$KH_PILOT_ID" \
     --project "$KH_PILOT_REPO" \
     > "$KH_PILOT_REPORTS/baseline/environment-capture.json"
   ```

4. **[W-REPORT]** 执行静态 Repository Intake：

   ```bash
   "$KH_BIN" repository analyze "$KH_PILOT_REPO" \
     --environment "$KH_PILOT_ID" \
     --output-root "$KH_PILOT_REPORTS/baseline" \
     > "$KH_PILOT_REPORTS/baseline/repository-intake-command.json"
   ```

5. 按详细手册 Gate G～I 验证真实 Workspace、五类最小 Context、Project Query/Skill 和 MCP。默认不包含 raw logs 或 paper fragments。
6. 分别完成 Literature、Code、Writing 至少一个有来源的烟雾查询。
7. **[R]** 会话结束重新记录项目 `HEAD/status`，与开始值比较。

第 1 周通过标准：项目无非预期变化；所有事实性响应有来源；没有运行代码、训练、安装依赖或写正式索引。

### 7.2 第 2 周：真实任务观察

目标：累计至少 30 条脱敏、可评价的真实观察：

| 类别 | 最低数量 |
|---|---:|
| 项目 Context / Query / Skill | 10 |
| Code | 10 |
| Literature | 5 |
| Writing | 5 |
| 合计 | 30 |

每次操作后追加一条第 9.2 节 JSONL 记录。不得为了凑数量重复同义查询；无命中也是有效观察，但必须如实标记。

Writing feedback 仅在项目所有者明确评价后写入。先从响应复制完整规范 `writing:` ID，再执行：

```bash
# [W-STATE] 示例；label 必须是项目所有者的真实选择。
"$KH_BIN" writing-v2 feedback '<writing:id>' '<label>'
"$KH_BIN" writing-v2 feedback-status
```

允许标签以当前 CLI/Schema 为准；Codex 不猜测或批量生成反馈。

### 7.3 第 3 周：问题聚类与候选改进

将观察分类为：

- `data_missing`：知识源确实没有所需材料；
- `retrieval_recall`：正确材料存在但没有进入 Top-K；
- `ranking`：正确材料存在但排序过低；
- `version_or_symbol`：库版本或符号错误；
- `source_missing`：结论缺少可追踪来源；
- `service_availability`：超时、readiness 或依赖服务问题；
- `interaction`：命令、Schema 或提示方式不清晰；
- `privacy_or_boundary`：越权、敏感内容或写入边界问题。

同类问题满足任一条件才立项：出现至少 3 次，或占该类别样本至少 10%。P0/P1 不受次数门槛限制，立即停止处理。

允许的改进验证：

- **[R]** 配置和源数据检查；
- **[W-REPORT]** offline/live evaluation；
- **[W-STATE]** 经批准的 dry-run 或隔离 candidate；
- 不切换正式 alias，不删除旧 collection。

以下仅作为 **[H] 建议流程**记录，首轮不执行：

```bash
knowledgehub index bootstrap-candidate code <candidate-collection>
knowledgehub index stage code <candidate-collection> --release-manifest <release.json>
knowledgehub index validate-candidate code <candidate-collection>
knowledgehub index snapshot code
knowledgehub index promote code --yes
knowledgehub index rollback-alias code --yes
```

任何 candidate 都需要独立任务、精确名称、完整回归和单独批准。

### 7.4 第 4 周：回归与复盘

**[W-REPORT]** 重跑固定 evaluation：

```bash
"$KH_BIN" evaluate run --mode offline --profile v2 \
  --output "$KH_PILOT_REPORTS/weekly/week-4-eval-offline-v2.json"
"$KH_BIN" evaluate run --mode live --profile v2 \
  --output "$KH_PILOT_REPORTS/weekly/week-4-eval-live-v2.json"
"$KH_BIN" evaluate compare \
  "$KH_PILOT_REPORTS/baseline/eval-live-v2.json" \
  "$KH_PILOT_REPORTS/weekly/week-4-eval-live-v2.json" \
  --thresholds "$KH_REPO/configs/evaluation/v2.yaml" \
  --output "$KH_PILOT_REPORTS/weekly/week-4-live-comparison.json"
```

同时重跑：release validate、validate all、dependencies offline、alias status、feedback status 和 readiness。最终只能选择一个结论：

1. **维持冻结版本**：运行稳定，没有足够证据支持改变；
2. **继续收集数据**：样本或问题模式不足，进入下一观察周期；
3. **准备候选发布**：问题重复、修复有效、门槛无回退；另建任务和审批，不在本 Pilot 自动发布。

## 8. 日常与周期维护

### 每个 Pilot 会话

- 记录项目开始/结束 `HEAD` 和 status；
- 检查 MCP/Search readiness；
- 追加脱敏查询观察；
- 发现 P0/P1 时立即停止，不自动修复。

### 每周

- `release validate`、`validate all`、alias status；
- 检查 systemd timer、失败任务、磁盘和最近错误日志；
- 汇总样本数量、来源完整率、失败分类和 p50/p95 延迟；
- 生成 `weekly/week-N-review.md`。

### 每月或候选发布前

- offline/live evaluation 与基线比较；
- feedback integrity 审计；
- 快照和恢复方案人工演练计划；
- 审查未引用 artifact 和旧 snapshot，但只生成清理计划；
- promote、rollback、clean、prune 继续要求单独批准。

## 9. 记录模板

### 9.1 目录结构

```text
<KH_PILOT_REPORTS>/
├── baseline/
├── daily/
│   └── YYYY-MM-DD.md
├── queries/
│   └── query-observations.jsonl
├── issues/
│   └── issues.md
├── weekly/
│   └── week-N-review.md
└── final-report.md
```

### 9.2 查询观察 JSONL

每行一个 JSON 对象，不写多行正文：

```json
{"timestamp":"<UTC ISO-8601>","cycle_id":"<cycle>","channel":"cli|mcp|search-api","operation":"project_context|project_query|project_skill|query","knowledge_base":"project|code|literature|writing","query_sha256":"<sha256>","query_summary":"<不含私人正文的短摘要>","intent":"<intent>","filters":{},"evidence_ids":[],"sources":[],"latency_ms":0,"useful":"yes|partial|no|unrated","source_complete":true,"failure_category":"none|data_missing|retrieval_recall|ranking|version_or_symbol|source_missing|service_availability|interaction|privacy_or_boundary","notes":"<脱敏备注>"}
```

原始查询含私有信息时，仅保存本地计算的 SHA-256 和不具可逆性的短摘要。

### 9.3 每日记录

```markdown
# YYYY-MM-DD Pilot 记录

- KnowledgeHub commit：
- 项目开始/结束 commit：
- 项目开始/结束 dirty 状态：
- health/readiness：
- 今日观察数量及分类：
- P0/P1/P2/P3：
- 非预期写入：无/有（说明）
- 敏感信息检查：通过/失败
- 下一步：
```

### 9.4 问题记录

```markdown
## ISSUE-<编号>

- 首次/最近出现：
- 严重度：P0/P1/P2/P3
- 类别：
- 影响样本和比例：
- 复现条件：
- 证据 ID/报告路径：
- 是否达到立项门槛：
- 临时规避：
- 候选改进：
- 回归要求：
- 状态：open/blocked/observing/closed
```

### 9.5 周报

```markdown
# Week N Review

- 有效观察总数及四类分布：
- 有来源结果比例：
- useful/partial/no/unrated：
- p50/p95 延迟：
- 各失败类别数量：
- 新增 P0/P1/P2/P3：
- 完整性、alias、readiness：
- 本周批准的写入：
- 未执行的高风险动作：
- 下周计划：
```

### 9.6 最终报告

```markdown
# Controlled Pilot Final Report

## 基线
- KnowledgeHub / 项目 commit：
- 四周起止时间：
- 正式 collection/alias：

## 数据
- 样本数量与分布：
- 来源完整率：
- 成功率和 p50/p95：
- feedback integrity：

## 稳定性与安全
- P0/P1：
- 完整性失败：
- 非预期项目或正式索引写入：
- 隐私事件：

## 回归
- offline/live compare：
- 未通过门槛：

## 结论
- 维持冻结版本 / 继续收集数据 / 准备候选发布
- 证据：
- 下一周期或候选任务：
- 明确未执行的动作：
```

## 10. 首轮通过标准

以下条件必须同时满足：

- P0、P1 均为 0；
- 项目没有 KnowledgeHub/Codex 导致的文件或 Git 变化；
- 正式 collection/alias 非预期变化为 0；
- 固定 evaluation 不低于 `configs/evaluation/v2.yaml` 门槛；
- 有效观察至少 30 条，并达到第 7.2 节分布；
- 有来源的事实性结果比例至少 95%；
- 私有数据和凭据泄漏为 0；
- release、integrity、alias 和 readiness 在期末全绿；
- 最终报告列明所有写入、例外和未执行高风险动作。

样本不足或不足以形成重复模式时，结论只能是“继续收集数据”，不能为了按期结束而降低门槛。

## 11. 立即停止条件

出现以下任一情况立即停止并记录为 FAIL 或 BLOCKED：

- release 或完整性验证失败；
- MCP/Search `readyz` failed，或持续 degraded 影响观察；
- token、密钥、私人全文、数据样本或未公开源码进入报告；
- KnowledgeHub/Codex 非预期修改真实项目；
- 真实项目状态写入 `state/fixtures` 或 Fixture namespace；
- 正式 collection/alias 发生未经批准的变化；
- 命令尝试 import/执行目标项目、安装依赖、下载模型或运行训练；
- MCP 获得目标项目或真实状态根的非必要写权限；
- 没有来源的推断被写成项目事实；
- 路径穿越或未知 Workspace 没有 fail-closed。

停止后只保留脱敏证据；不自动重试高风险命令，不 reset 项目，不删除状态，不执行 rollback。恢复和清理必须另建任务并获得明确批准。

## 12. 当前下一步

1. 由项目所有者创建私有真实项目并完成首个提交；
2. 按第 3～5 节建立隔离路径和 Day 0 基线；
3. 把第 6.2 节任务交给 Codex，实现并验收 Gate F；
4. Gate F 通过后，按[真实项目 Pilot 手册](REAL_PROJECT_PILOT.zh-CN.md)完成 Gate G～J；
5. 所有启动门槛通过后的下一个完整工作日开始第 1 周；
6. 四周结束按证据选择维持、继续观察或另行准备候选发布。
