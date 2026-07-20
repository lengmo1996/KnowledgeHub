# KnowledgeHub V3 真实科研项目 Pilot 逐步实施手册

状态：**Gate F 已实现；Gate G 仍需通过运行时路径与权限检查**
适用基线：KnowledgeHub commit `129272a` 或其后继版本  
最后核对日期：2026-07-20

## 1. 目的和当前边界

本手册用于在真实科研项目建立后，按固定顺序完成第一轮只读 Pilot。第一轮只验证：

- 目标仓库身份、依赖和环境能否被安全读取；
- Literature / Code / Writing 三个正式知识库能否返回可追踪证据；
- 真实 Workspace、项目上下文和 MCP 项目工具能否在独立状态根中工作；
- 全程不修改项目源码、不执行训练、不写正式向量索引、不删除任何项目数据。

Gate F 现在提供显式真实项目 opt-in。Fixture 默认值和 Experiment Schema 仍保持
Fixture-only；真实 Workspace 只允许 `workspace_type: project` 与
`data_scope: private|project`。Project Query/Skill 按 Workspace 类型选择 Router：Fixture
继续使用 `FixtureKnowledgeRouter`，真实项目只能使用正式 `HubQueryService` 的只读 Router。

这不自动放行 Gate G。真实 Workspace 创建仍必须同时提供 `--allow-real-project`、独立
`--state-root` 和固定 `--repository-root`，并通过路径、所有权、权限和 namespace 检查。

## 2. Pilot 目录和变量

以下示例假定：

- KnowledgeHub 位于 `/home/lengmo/KnowledgeHub`；
- conda 环境是 `rag`；
- 真实项目位于 `/absolute/path/to/real-project`；
- Workspace ID 使用小写字母、数字、点、下划线或连字符，长度 3～80。

先进入 KnowledgeHub，并把占位值替换为真实值：

```bash
cd /home/lengmo/KnowledgeHub
export KH_PILOT_REPO=/absolute/path/to/real-project
export KH_PILOT_ID=my-real-project
export KH_PILOT_STATE=/data/KnowledgeHub/projects/$KH_PILOT_ID
export KH_PILOT_REPORTS=/data/KnowledgeHub/reports/real-project-pilot/$KH_PILOT_ID
export KH_BIN=/home/lengmo/anaconda3/envs/rag/bin/knowledgehub
```

逐项确认变量，输出中不能再出现 `/absolute/path/to/real-project`：

```bash
test -x "$KH_BIN"
test -d "$KH_PILOT_REPO"
test "$KH_PILOT_REPO" != /home/lengmo/KnowledgeHub
printf '%s\n' "$KH_PILOT_ID" | rg '^[a-z0-9][a-z0-9._-]{2,79}$'
printf 'repo=%s\nid=%s\nstate=%s\nreports=%s\n' \
  "$KH_PILOT_REPO" "$KH_PILOT_ID" "$KH_PILOT_STATE" "$KH_PILOT_REPORTS"
```

任一命令失败就停止，不要用 `sudo` 绕过路径或权限问题。

## 3. Gate A：项目仓库准入

### 3.1 确认它是独立 Git 仓库

```bash
git -C "$KH_PILOT_REPO" rev-parse --show-toplevel
git -C "$KH_PILOT_REPO" status --short
git -C "$KH_PILOT_REPO" rev-parse HEAD
git -C "$KH_PILOT_REPO" branch --show-current
```

记录 `HEAD`。工作区可以有用户自己的未提交改动，但 Pilot 必须把它标记为 dirty，且不得自动清理、stash、reset 或提交。

如果还没有首个 commit，先在真实项目自己的工作流中完成初始化和首个可恢复提交，再回来执行 Pilot。不要让 KnowledgeHub 替项目创建提交。

### 3.2 排除不适合首轮 Pilot 的仓库

出现以下任一情况时停止：

- 仓库中含有不能被当前主机账户只读访问的受限数据；
- 仅仅扫描文件名或依赖声明也违反项目许可；
- 仓库根目录是挂载中的训练输出、数据集或模型权重目录；
- 项目必须先运行安装脚本、下载模型或执行任意代码才能识别；
- 没有可固定的 Git commit。

检查明显的大文件和符号链接；这两条命令只读：

```bash
find "$KH_PILOT_REPO" -xdev -type f -size +500M -print
find "$KH_PILOT_REPO" -xdev -type l -print
```

发现大文件不等于必须失败，但要确认 Repository Intake 不需要读取数据集、checkpoint 或输出目录。必要时先在项目中完善忽略和目录边界，再继续。

### 3.3 建立 Pilot 专用输出目录

这些目录只保存 KnowledgeHub 生成的状态和报告，不放进真实项目仓库：

```bash
install -d -m 0700 "$KH_PILOT_STATE" "$KH_PILOT_REPORTS"
test -w "$KH_PILOT_STATE"
test -w "$KH_PILOT_REPORTS"
```

不得使用以下路径作为 Pilot 状态根：

```text
/home/lengmo/KnowledgeHub/state/fixtures
/data/KnowledgeHub/zotero
真实项目仓库内部
任何正式 Qdrant collection 的存储目录
```

## 4. Gate B：KnowledgeHub 基线和 MCP

### 4.1 验证本地代码基线

```bash
cd /home/lengmo/KnowledgeHub
git rev-parse HEAD
git status --short
"$KH_BIN" release validate
"$KH_BIN" mcp validate
"$KH_BIN" mcp tools
```

预期：

- KnowledgeHub 自己的 Git 状态只包含你已知的本地改动；
- release 与 MCP schema 验证成功；
- MCP 工具清单中包含 `knowledge_project_query` 和 `knowledge_project_skill`；
- 工具总数至少为 V3 基线的 17 个，后继版本可以更多。

### 4.2 检查运行服务

在 KnowledgeHub 服务器执行：

```bash
systemctl is-active knowledgehub-rag-core.service
systemctl is-active knowledgehub-mcp-lan.service
systemctl is-active knowledgehub-mcp-tailscale.service
curl -fsS http://10.249.44.27:8091/healthz
curl -fsS http://10.249.44.27:8091/readyz
curl -fsS http://127.0.0.1:8092/healthz
curl -fsS http://127.0.0.1:8092/readyz
```

`healthz` 成功只说明进程存活；必须同时检查 `readyz`。如果 readiness 不是 ready，先修复依赖，不进入真实 Pilot。

只有在代码、环境文件或项目状态根配置改变后才重启 MCP：

```bash
sudo systemctl restart knowledgehub-mcp-lan.service
sudo systemctl restart knowledgehub-mcp-tailscale.service
systemctl is-active knowledgehub-mcp-lan.service
systemctl is-active knowledgehub-mcp-tailscale.service
curl -fsS http://10.249.44.27:8091/readyz
curl -fsS http://127.0.0.1:8092/readyz
```

两个 MCP unit 相互独立。某一个失败时先查看它的日志，不要反复同时重启：

```bash
journalctl -u knowledgehub-mcp-lan.service -n 100 --no-pager
journalctl -u knowledgehub-mcp-tailscale.service -n 100 --no-pager
```

## 5. Gate C：捕获真实环境

先 dry-run。它读取当前 `rag` Python 环境和项目依赖文件，不执行目标项目代码：

```bash
"$KH_BIN" environment capture \
  --name "$KH_PILOT_ID" \
  --project "$KH_PILOT_REPO" \
  --dry-run \
  > "$KH_PILOT_REPORTS/environment-capture-dry-run.json"
/home/lengmo/anaconda3/envs/rag/bin/python -m json.tool \
  "$KH_PILOT_REPORTS/environment-capture-dry-run.json"
```

检查输出中的：

- `project_root` 是目标项目；
- Python executable 是预期环境；
- `project_files` 只列出依赖声明文件及其哈希；
- URL、token、key、password 等敏感值已经脱敏；
- GPU/CUDA 信息符合当前机器。

确认后保存环境快照：

```bash
"$KH_BIN" environment capture \
  --name "$KH_PILOT_ID" \
  --project "$KH_PILOT_REPO" \
  > "$KH_PILOT_REPORTS/environment-capture.json"
```

该命令会在 KnowledgeHub Code data root 的 `state/environments/` 下写入 JSON，不会写目标仓库。记录命令输出中的 `output` 绝对路径，后续 Intake 使用同名环境。

## 6. Gate D：只读 Repository Intake

执行静态分析：

```bash
"$KH_BIN" repository analyze "$KH_PILOT_REPO" \
  --environment "$KH_PILOT_ID" \
  --output-root "$KH_PILOT_REPORTS" \
  > "$KH_PILOT_REPORTS/repository-intake-command.json"
```

该命令只解析依赖声明、Python AST、入口脚本、测试和配置，不 import 项目、不安装依赖、不执行训练。

查找并查看生成结果：

```bash
find "$KH_PILOT_REPORTS" -maxdepth 3 -type f -print | sort
find "$KH_PILOT_REPORTS" -name repository_profile.json -print
find "$KH_PILOT_REPORTS" -name compatibility_matrix.json -print
find "$KH_PILOT_REPORTS" -name compatibility_report.md -print
```

人工检查：

- repository commit 与第 3.1 节记录的 commit 一致；
- dirty 状态如实记录；
- Python 文件数量是否被 5,000 文件上限截断；
- 依赖冲突是 runtime、dev 还是 optional scope；
- 未把数据集、checkpoint、密钥或私有日志复制进报告；
- 项目入口、训练/推理脚本和测试识别是否合理。

若发现敏感内容已进入输出，立即停止，限制报告目录权限并人工处理；不要把该报告提交到 Git。

## 7. Gate E：三个正式知识库的只读烟雾测试

下面的查询不会修改索引。把问题替换为项目真正依赖的库、研究问题和论文写作任务；每类至少保存一个有来源的成功响应。

### 7.1 Code RAG

```bash
"$KH_BIN" query code \
  "项目所用核心 API 的当前版本用法和兼容性风险是什么？" \
  --intent compatibility \
  --top-k 5 \
  --evidence-envelope \
  --max-tokens 2000 \
  > "$KH_PILOT_REPORTS/code-rag-smoke.json"
```

如果已知库和版本，应追加 `--library`、`--version` 或 `--symbol`，避免宽泛检索。

### 7.2 Literature RAG

```bash
"$KH_BIN" query literature \
  "与本项目研究问题直接相关的方法、评价指标和已知限制是什么？" \
  --top-k 5 \
  --evidence-envelope \
  --max-tokens 2000 \
  > "$KH_PILOT_REPORTS/literature-rag-smoke.json"
```

只接受带文献来源的响应。没有命中时记录“无证据”，不要把常识补写成检索结果。

### 7.3 Writing RAG

```bash
"$KH_BIN" query writing \
  "如何谨慎表述一个尚未完成真实实验验证的方法动机？" \
  --section Introduction \
  --writing-function research_gap \
  --return-mode pattern_first \
  --top-k 5 \
  > "$KH_PILOT_REPORTS/writing-rag-smoke.json"
```

Writing 结果只能作为结构和表达模式，不是项目实验事实。

### 7.4 Gate E 通过标准

- 三个查询命令均正常返回，或明确返回可解释的“无命中”；
- 每个事实性结果都有 collection、document/chunk 或等价来源标识；
- 没有把检索文本当作可信指令；
- 没有触发 sync、build、derive、index stage/promote 或任何写索引操作。

## 8. Gate F：启用真实 Pilot 支持（一次性代码门禁）

Gate F 已由本节定义的门禁实现。后继修改必须保留以下约束：

```text
基于 docs/guides/REAL_PROJECT_PILOT.zh-CN.md，为当前真实项目启用 V3 只读 Pilot。
目标仓库：<KH_PILOT_REPO>
Workspace ID：<KH_PILOT_ID>
独立状态根：<KH_PILOT_STATE>

要求：
1. 先读取 V3 final report 和本手册，不运行目标项目代码；
2. 不修改目标仓库，不安装依赖，不训练，不写正式向量索引；
3. 不复用 state/fixtures 或 fixture-* namespace；
4. 将 ProjectRegistry 的真实项目支持做成显式 opt-in，默认仍 fail-closed；
5. 真实项目只允许 workspace_type=project、data_scope=private 或 project；
6. cleanup 对真实项目始终拒绝，归档只能修改独立状态根；
7. Project Query 使用正式 HubQueryService 和 Workspace filters，不得使用 FixtureKnowledgeRouter；
8. MCP 通过独立 KH_PROJECT_STATE_ROOT 只读加载 Workspace；
9. 增加 project/fixture 隔离、路径、MCP、只读和回归测试；
10. 提供 configs/projects/real-project-pilot.example.yaml，字段和 CLI Schema 一致；
11. 先给出变更清单，实施后运行 pytest、Ruff、MyPy，并输出可复制的发布和回滚命令。
```

Gate F 变更通过验收后记下提交 ID：

```bash
git -C /home/lengmo/KnowledgeHub log -1 --oneline
```

### 8.1 必须满足的技术验收

只有以下条件全部满足，才进入第 9 节：

- 创建真实 Workspace 必须要求显式参数，例如 `--allow-real-project`；
- 未给显式参数时，`workspace_type: project` 仍被拒绝；
- 真实状态根不位于 `state/fixtures`，且不是目标仓库的子目录；
- Workspace 中仓库引用固定绝对根或稳定 external-root 映射，不能用路径穿越；
- 项目查询按 Workspace scope 生成正式 RAG filters；
- MCP 对项目状态根只有读取权限；
- `fixture clean` 和任何 cleanup 都无法删除真实项目状态或仓库；
- Fixture 既有测试继续通过；
- 新增的真实 Pilot 测试不执行任意目标仓库代码；
- `pytest`、Ruff、MyPy 全部通过。

如果实现输出的实际 CLI 与第 9～12 节占位接口不同，应由该提交同步修订本手册，不能靠操作者猜测参数。

## 9. Gate G：创建真实 Workspace

只有 `knowledgehub workspace create --help` 已显示 `--allow-real-project`，且状态根实际可写时才能执行本节。

先确认接口：

```bash
"$KH_BIN" workspace create --help | rg -- '--allow-real-project'
```

从 Gate F 提供的模板创建配置，并填写所有占位值：

```bash
cp configs/projects/real-project-pilot.example.yaml "$KH_PILOT_STATE/workspace.yaml"
nano "$KH_PILOT_STATE/workspace.yaml"
```

模板至少包含：

```yaml
schema_version: "3.0"
workspace_id: my-real-project
name: My Real Project
description: Read-only first pilot
workspace_type: project
data_scope: private
status: active
research:
  question: replace me
  hypotheses:
    - replace me
repositories:
  - repository_id: primary-repository
    path: .
environments:
  default: my-real-project
knowledge:
  literature:
    namespace: production-literature
    filters: {}
  code:
    namespace: production-code
    filters: {}
  writing:
    namespace: production-writing
    filters: {}
created_at: "<UTC ISO-8601>"
updated_at: "<UTC ISO-8601>"
```

创建前人工填写 research、环境 ID 和三个知识范围；禁止使用 `fixture-*` namespace，禁止定义 `write_target`。

按 Gate F 交付的实际命令创建、验证和导出。目标形式如下：

```bash
"$KH_BIN" workspace create "$KH_PILOT_STATE/workspace.yaml" \
  --state-root "$KH_PILOT_STATE/registry" \
  --repository-root "$KH_PILOT_REPO" \
  --allow-real-project
"$KH_BIN" workspace validate "$KH_PILOT_ID" \
  --state-root "$KH_PILOT_STATE/registry" \
  --repository-root "$KH_PILOT_REPO"
"$KH_BIN" workspace export "$KH_PILOT_ID" \
  --state-root "$KH_PILOT_STATE/registry" \
  --output "$KH_PILOT_REPORTS/workspace-export.json"
```

验证结果必须是 `valid: true`。警告必须逐条解释，不能通过手工改 JSON 跳过 Schema。

## 10. Gate H：项目 Context、Query 和 Skill

先只构建不含原始日志和论文片段的最小上下文：

```bash
"$KH_BIN" project context "$KH_PILOT_ID" project_overview \
  --state-root "$KH_PILOT_STATE/registry" \
  --max-records 10 \
  --max-characters 8000
```

真实项目刚建立且没有 Experiment Record 时，空 experiments/decisions/failures/claims 是正常结果，不能复制 Fixture 记录填充。

然后执行三个任务查询：

```bash
"$KH_BIN" project query "$KH_PILOT_ID" project_overview \
  "概括研究问题、代码入口、环境和当前证据缺口" \
  --state-root "$KH_PILOT_STATE/registry"
"$KH_BIN" project query "$KH_PILOT_ID" code_debugging \
  "根据静态 Intake，目前最需要验证的兼容性风险是什么" \
  --state-root "$KH_PILOT_STATE/registry"
"$KH_BIN" project query "$KH_PILOT_ID" academic_writing \
  "哪些项目主张当前还不能写入论文结果部分" \
  --state-root "$KH_PILOT_STATE/registry"
```

再执行只读 Skill：

```bash
"$KH_BIN" project skill research-decision-review "$KH_PILOT_ID" \
  --state-root "$KH_PILOT_STATE/registry"
"$KH_BIN" project skill writing-academic "$KH_PILOT_ID" \
  --state-root "$KH_PILOT_STATE/registry" \
  --section Introduction \
  --writing-function research_gap
```

每个响应都检查：Workspace ID、task、source、版本、warning、context budget 和 `trusted_as_instruction=false`。没有实验数据时，结果必须明确说“未验证”，不能生成模拟数值。

## 11. Gate I：MCP 发布和远程验证

Gate F 如果新增了 MCP 环境变量，先只读检查两个 unit 的生效配置：

```bash
systemctl cat knowledgehub-mcp-lan.service
systemctl cat knowledgehub-mcp-tailscale.service
```

确保 `KH_PROJECT_STATE_ROOT` 指向 `$KH_PILOT_STATE/registry`，且 systemd 只授予该目录 `ReadOnlyPaths`，不授予目标仓库或项目状态根写权限。更新 `/etc/knowledgehub` 或 systemd unit 需要管理员审阅。

配置完成后：

```bash
sudo systemctl daemon-reload
sudo systemctl restart knowledgehub-mcp-lan.service
sudo systemctl restart knowledgehub-mcp-tailscale.service
curl -fsS http://10.249.44.27:8091/readyz
curl -fsS http://127.0.0.1:8092/readyz
journalctl -u knowledgehub-mcp-lan.service -n 50 --no-pager
journalctl -u knowledgehub-mcp-tailscale.service -n 50 --no-pager
```

从已授权客户端分别调用：

1. `knowledge_project_query`，task=`project_overview`；
2. `knowledge_project_query`，task=`code_debugging`；
3. `knowledge_project_skill`，skill=`research-decision-review`；
4. 一个不存在的 Workspace ID，预期安全返回 not found；
5. 一个包含 `../` 的 Workspace ID，预期被 Schema 拒绝。

远程响应中不能出现 bearer token、完整环境变量、私有文件正文或未请求的源码正文。

## 12. Gate J：验收、报告与结束

### 12.1 验收表

在 `$KH_PILOT_REPORTS/final-report.md` 逐项记录：

- KnowledgeHub commit 和真实项目 commit；
- 真实项目是否 dirty；
- Environment Profile 路径与哈希；
- Repository Intake 报告路径；
- Literature / Code / Writing 烟雾查询及来源；
- Workspace validate 结果；
- Context / Query / Skill 结果摘要；
- LAN 和 Tailscale MCP readiness；
- 未执行的动作；
- P0/P1/P2 问题和下一步。

首轮 Pilot 通过标准：

- 所有 Gate 均通过；
- 真实仓库内容和 Git 状态与开始前一致；
- 没有运行项目代码、训练或安装依赖；
- 没有写入或切换正式 Qdrant collection/alias；
- 没有把真实数据写入 Fixture Registry；
- 所有项目级结论都有来源，缺少实验时明确标记 unsupported/unverified；
- MCP 仍为只读，路径穿越和未知 Workspace fail-closed。

### 12.2 结束时确认目标仓库未被修改

```bash
git -C "$KH_PILOT_REPO" rev-parse HEAD
git -C "$KH_PILOT_REPO" status --short
```

与第 3.1 节对比。若发生非预期变化，Pilot 判定 FAIL，保留报告和日志，不自动 reset。

### 12.3 归档而不是删除

第一轮结束只归档 Workspace 元数据；不要清理真实项目：

```bash
"$KH_BIN" workspace archive "$KH_PILOT_ID" \
  --state-root "$KH_PILOT_STATE/registry"
```

`fixture clean` 永远不能用于真实 Workspace。需要删除 Pilot 状态时，先人工审计精确路径并另行批准；本手册不提供递归删除命令。

## 13. 立即停止条件

出现以下任一情况立即停止并记录为 FAIL 或 BLOCKED：

- 当前版本仍没有显式真实项目 opt-in，却尝试创建 project Workspace；
- 任何命令要修改真实仓库、安装依赖、下载模型或运行训练；
- 状态根落入 `state/fixtures`、真实仓库或正式知识库数据目录；
- Workspace namespace 仍以 `fixture-` 开头；
- Query/Skill 仍实例化 `FixtureKnowledgeRouter`；
- readiness degraded/failed；
- 检索结果没有来源却被写成事实；
- MCP 获得真实项目或状态根的非必要写权限；
- 目标项目 Git commit 或 dirty 状态发生非预期变化；
- 发现 token、密钥、私人论文全文或受限数据进入日志/报告。

## 14. 当前应做什么

在 Gate G 运行时条件尚未满足时，不执行创建命令。保留：

- `state/fixtures/fixture-vision-project` 作为回归基线；
- `reports/v3_fixture/` 的验收证据；
- 本手册作为真实项目接入的唯一顺序入口。

真实项目建立后，从第 2 节开始逐 Gate 执行；使用
`configs/projects/real-project-pilot.example.yaml`，不要复制 Fixture 配置。
