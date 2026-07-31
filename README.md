# Agent Team Workflow

[English](README_EN.md)

Agent Team Workflow 是一个面向 Codex、Claude Code 与 CC-Panes 的持久化多 Agent
开发工作流插件。它把需求与架构、实现、独立审查分离为三个稳定角色，并将关键状态
写入项目内的 `.codex/`，使长任务在进程重启、上下文压缩或电脑重启后仍可恢复。

> 当前版本：**0.2.0 alpha**。默认是三个 Codex 角色。Claude Code Leader 已提供
> 适配基础，但只有用户明确批准 `claude-leader` 拓扑后才会启用。

## 核心原则

- **三个角色进程**：Leader、Executor Mother、Independent Reviewer；不创建第四个
  正式角色。
- **界面布局与协议分离**：窗格、标签、标题、几何位置和分屏方式完全由 CC-Panes
  与用户管理，不参与身份验证或授权。
- **用户只与 Leader 交流**：Leader 负责澄清需求、形成规格、派发实现、路由审查和
  汇总结果，用户无需在多个会话之间复制提示词。
- **默认低成本**：`lean` 模式限制规格修订、内部 Agent、审查轮次、失败重试和全量
  测试次数，避免无止境的 Spec 与 Review 循环。
- **模型由用户控制**：插件不自动选择、升级、降级或路由模型，也不覆盖 Provider、
  reasoning effort 或用户在会话中的手动切换。
- **可恢复状态**：规格、执行计划、审查结论、角色会话与绑定均有持久记录，原始聊天
  历史不是唯一事实来源。

`codex-three-pane` 仍作为默认拓扑的兼容名称保留，但它只是历史命名，不代表必须使用
三个窗格，也不规定左、右上、右下等布局。

## 三个角色

| 角色 | 默认 CLI | 职责 |
|---|---|---|
| Leader | Codex | 与用户讨论需求和架构，维护规格与恢复状态，调度 Executor 和 Reviewer |
| Executor Mother | Codex | 读取已批准规格，完成实现、集成和针对性验证，冻结待审查快照 |
| Independent Reviewer | Codex | 只读检查冻结快照，验证验收条件，不直接修复代码或扩展范围 |

可选的 `claude-leader` 拓扑只替换 Leader CLI；Executor 与 Reviewer 仍为 Codex。
安装 Claude 兼容文件或发现 Claude CLI 都不等于授权切换拓扑。

## 工作流

```text
用户 <-> Leader
           |
           | 已批准 .codex/spec.md
           v
       Executor Mother
           |
           | 冻结 fingerprint + 验证证据
           v
   Independent Reviewer
           |
           +-- PASS -> DONE
           +-- 范围内 P0/P1 -> Executor 定向修复 -> 最多一次聚焦复审
```

一次正常任务经历：

1. Leader 将想法整理为简短规格，状态依次为 `DISCOVERY -> DESIGNING -> PROPOSED`。
2. 用户明确批准后，规格进入 `APPROVED`；批准前 Executor 不开始实现。
3. Executor 执行实现和针对性测试，记录计划、证据与稳定 fingerprint。
4. Reviewer 对精确 fingerprint 做一次独立审查，不扩展已批准范围。
5. 通过后状态进入 `DONE`，三个角色保持待命，可继续处理下一个任务。

## 安装

### Codex

```powershell
codex plugin marketplace add meizhouyu666/agent-team-workflow
codex plugin add agent-team-workflow@agent-team-workflow
```

### Claude Code

```powershell
claude plugin marketplace add meizhouyu666/agent-team-workflow
claude plugin install agent-team-workflow@agent-team-workflow
```

安装或升级不会自动启动角色、切换拓扑、修改权限或迁移现有绑定。CLI 目前不能热加载
插件与 Provider/API 配置，因此安装后需要新建顶层会话，才能加载新版 Skill。

## 快速开始

在一个新的 Codex Leader 会话中输入：

```text
[ATW][Leader][<项目目录名>]
使用 $lead-agent-workflow 接管当前项目，采用默认三 Codex 角色拓扑。
先和我讨论需求与架构，在我明确批准规格前不要开始实现。
```

三个入口 Skill：

- `$lead-agent-workflow`：用户入口、规格、架构、派发、恢复和最终汇报。
- `$orchestrate-agent-team`：Executor Mother 的实现与验证流程。
- `$review-agent-work`：Independent Reviewer 的只读审查流程。

角色权威身份由以下组合确定：

```text
project_root + role + session_id + binding_id
```

CLI、Leader generation、父绑定、能力声明与验证时间作为附加证据。重复角色、会话或
绑定会被拒绝；pane、tab 和 layout 不在身份合同内。

CC-Panes 中的三个角色使用固定的可见标题：

- `[ATW][Leader][<项目目录名>]`
- `[ATW][Executor][<项目目录名>]`
- `[ATW][Reviewer][<项目目录名>]`

Leader 在启动、恢复和重新绑定角色时同时设置 `launch_task.title` 与 TaskBinding 标题；
新会话的 prompt 第一行也使用同一标题。标题只用于人工识别，绝不参与身份验证或授权。
如果还想让 Codex 自身的恢复列表更清楚，可以在每个角色中执行一次 `/rename`，使用同名标题。

## 低成本模式

`lean` 是默认模式，当前硬限制为：

| 行为 | 上限 |
|---|---:|
| 规格修订 | 1 |
| Executor 内部 Agent | 0 |
| 实现审查 | 1 |
| 聚焦复审 | 1 |
| 同一失败原因重试 | 2 |
| 全量测试运行 | 1 |
| 未批准的范围扩展 | 0 |

限制状态保存在 `.codex/guards/<run-id>.json`，消费操作使用稳定 operation key，重复
调用保持幂等。P2 默认不阻塞 lean 交付，除非它证明已批准的验收条件被违反。

`assurance` 仅在用户明确选择时启用，用于安全、权限、不可逆迁移、完整兼容矩阵、
故障注入或严格 E2E 等高风险任务。行为 guard 约束遵守协议的 Skill 与 adapter，
并不是针对任意外部进程的安全沙箱。

## 重开角色与整队轮换

当 Codex CLI 无法热加载 Provider/API 配置，或角色进程需要重启时，可以直接对 Leader
说“重开 Executor”“重开 Reviewer”或“整队轮换”。Leader 使用蓝绿替换：

1. 持久化当前规格、计划、绑定和 replay ledger，并冻结目标角色的新工作。
2. 使用用户当前的 CC-Panes/Provider 配置启动一个替代候选。
3. 验证候选的 cwd、Git 根目录、Skill、角色身份、父绑定、generation 与能力。
4. 验证成功后才 rebind/reconcile，然后停止旧进程；失败时旧进程保持运行。

整队轮换顺序固定为 **Executor -> Reviewer -> Leader**，Leader 最后切换。若三个角色
进程全部死亡，插件无法自行启动；用户必须先在 CC-Panes 中手动启动一个新 Leader，
再由它读取持久状态并恢复另外两个角色。

## 项目状态文件

| 文件 | 所有者 | 用途 |
|---|---|---|
| `.codex/spec.md` | Leader | 目标、边界、架构、合同、风险和验收条件 |
| `.codex/leader-state.md` | Leader | 当前 run、角色会话与 binding、下一动作和恢复说明 |
| `.codex/plan.md` | Executor | 实现任务、验证证据、review scope 和 fingerprint |
| `.codex/review.md` | Reviewer | 当前审查结论、finding 和复核证据 |
| `.codex/guards/<run-id>.json` | 协议工具 | lean 行为额度和幂等消费记录 |
| `.codex/roles.json` | 可选 schema-1 拓扑 | 显式 Claude Leader 迁移后的权威角色描述 |

默认三 Codex 拓扑不会仅因安装 0.2.0 而创建 `.codex/roles.json` 或启动迁移。

## 兼容性与边界

- 当前主要运行环境是 Windows x64、PowerShell、Codex 与 CC-Panes。
- `compatibility.json` 中只有精确标记为 `SUPPORTED` 的组合可进入相应权威操作。
- Gemini、OpenCode、WSL/SSH、Linux 和 macOS 适配仍属于后续方向。
- CC-Panes 没有 compare-and-swap generation 原语；当前防护依赖遵守协议的进程、
  generation fencing、持久 journal 和立即 reconciliation。
- 完整三角色都停止时不存在自动恢复入口，这是外部 CLI 生命周期的硬边界。

## 仓库结构

```text
plugins/agent-team-workflow/
  .codex-plugin/plugin.json  插件清单
  skills/                    三个角色 Skill
  scripts/                   协议、fingerprint、guard 与测试 harness
  schemas/                   envelope、ledger、migration 与 role-state schema
  compatibility.json         adapter/CLI 兼容合同
docs/
  protocol.md                平台中立协议
  operator-guide.md          安装、恢复与运维说明
  adapters/codex-ccpanes.md  CC-Panes 参考 adapter
tests/                       标准库 unittest 契约测试
```

## 开发验证

```powershell
python -B -m unittest discover -s tests -p test_protocol_core.py -v
python -B -m unittest discover -s tests -p test_protocol_harness.py -v
python -B -m unittest discover -s tests -p test_packaging_contract.py -v
git diff --check
```

更完整的协议、adapter 和运维细节见：

- [平台中立协议](docs/protocol.md)
- [CC-Panes 参考 adapter](docs/adapters/codex-ccpanes.md)
- [操作指南](docs/operator-guide.md)
- [兼容性矩阵](plugins/agent-team-workflow/compatibility.json)

## License

[MIT](LICENSE)
