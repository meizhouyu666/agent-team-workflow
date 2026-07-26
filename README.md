# Agent Team Workflow

[English](README_EN.md)

一个面向长周期软件开发的多 Agent 工作流。它把架构讨论、实现编排和独立审查拆成三个长期角色，并使用项目内的持久化文件完成跨进程、跨上下文窗口交接。

> 当前版本为 **0.1.0 alpha**。Codex + CC-Panes 是首个参考实现；核心协议按未来可适配 Claude Code、Gemini、OpenCode 等 CLI 的方向设计。

## 为什么做这个项目

多 Agent 本身并不难，真正麻烦的是持续协调：

- 每次都要手工复制 Prompt；
- Agent 只依赖聊天上下文，窗口快满时容易丢失架构决策；
- 实现者自己审查自己，容易遗漏同一类问题；
- 多个项目并行时，目录、会话和交接文件很容易串线；
- Git Worktree、脏工作区和未跟踪文件经常被普通 diff 漏掉。

本项目把这些约束固化成可恢复的团队协议。

## 默认团队

~~~text
┌─────────────────────┬──────────────────────────┐
│ Leader              │ Executor Mother          │
│ 需求与架构讨论      │ 实现、子 Agent 编排与集成│
│                     ├──────────────────────────┤
│                     │ Independent Reviewer     │
│                     │ 独立、只读、证据化审查   │
└─────────────────────┴──────────────────────────┘
~~~

- **Leader**：直接和用户讨论，维护需求、架构、边界与持久化状态；不写应用代码。
- **Executor Mother**：只消费已批准的规格，按不重叠写入范围派发内部子 Agent，负责集成与测试。
- **Independent Reviewer**：不继承实现过程的推理，只针对冻结快照独立验收，不直接修复代码。

Executor 的内部子 Agent 不创建额外可见窗格。

## 核心流程

~~~text
DISCOVERY → DESIGNING → PROPOSED → APPROVED
                                      │
                                      ▼
PLANNING → IMPLEMENTING → TESTING → READY_FOR_REVIEW
                                      │
                                      ▼
                          PASS / REQUEST_CHANGES / STALE
~~~

关键决策保存在项目根目录的 `.codex/`：

- `.codex/spec.md`：Leader 拥有的需求与架构规格；
- `.codex/leader-state.md`：角色、会话、开放问题与下一步；
- `.codex/plan.md`：Executor 拥有的实现计划与验证证据；
- `.codex/review.md`：Reviewer 发布的权威审查结论；
- `.codex/leader-handoff.md`：Leader 换届时的恢复检查点。

聊天记录不是唯一记忆来源。新 Leader 必须能仅依靠这些文件和当前仓库状态完成恢复。

## 当前支持范围

| 环境 | 状态 |
|---|---|
| Codex + CC-Panes | 参考实现，实验性可用 |
| 独立 Codex CLI | 核心角色协议可用，缺少自动三窗格编排 |
| Claude Code | 计划适配 |
| Gemini CLI | 计划适配 |
| OpenCode | 计划适配 |

## 安装

要求：

- 支持 Plugin 的 Codex CLI；
- Git；
- Python 3.10+，用于确定性审查指纹；
- 若需要自动三窗格、PTY 投递和 TaskBinding，需要 CC-Panes MCP。

添加 Git marketplace：

~~~powershell
codex plugin marketplace add meizhouyu666/agent-team-workflow
codex plugin add agent-team-workflow@agent-team-workflow
~~~

安装或升级后，请开启一个新对话，让 Codex 重新加载 Skill。

## 使用

在项目的 Leader 会话中说：

~~~text
使用 $lead-agent-workflow 接管这个项目，先和我讨论需求与架构，
不要在规格批准前开始实现。
~~~

固定三角色团队建立后：

1. 用户只与 Leader 讨论；
2. Leader 自动维护规格与交接文件；
3. 用户批准规格后，Leader 唤醒 Executor；
4. Executor 完成集成和测试后冻结快照；
5. Leader 唤醒 Reviewer；
6. 实现问题回到 Executor，架构或范围问题回到 Leader；
7. Reviewer 对完全一致的快照给出 PASS 后才完成。

也可以单独调用：

- `$orchestrate-agent-team`：执行已批准规格；
- `$review-agent-work`：独立审查冻结实现快照。

## Git 与 Worktree 安全

- 每个角色启动后必须核验终端 cwd 和 `git rev-parse --show-toplevel`；
- Worktree 必须使用精确路径，不能用工作空间根目录代替；
- 不清理、不重置、不提交用户已有改动；
- 未跟踪但未忽略的文件必须进入审查快照；
- `.codex/**` 是协调状态，不进入产品实现指纹；
- 同一 Working Tree 同时只允许一个活动实现 Run。

确定性指纹工具：

~~~powershell
python plugins/agent-team-workflow/scripts/review_fingerprint.py --root . --json
~~~

限定审查范围时重复传入 `--path`：

~~~powershell
python plugins/agent-team-workflow/scripts/review_fingerprint.py --root E:\path\to\project --path internal/api --path tests
~~~

Executor 和 Reviewer 必须使用完全相同的命令、范围和排除项。

## 设计边界

平台无关协议见 [docs/protocol.md](docs/protocol.md)，Codex + CC-Panes 的参考适配见 [docs/adapters/codex-ccpanes.md](docs/adapters/codex-ccpanes.md)。

核心层只关心：

- 角色与权限；
- 状态机；
- 持久化交接；
- 冻结快照和审查结论；
- Leader 恢复与换届。

启动会话、发送消息、读取状态和绑定角色由适配器实现。

## 已知限制

- 尚未完成系统性的跨平台端到端测试；
- CC-Panes 新布局可能先显示成标签，随后异步稳定为窗格；
- Worktree 第一次启动可能出现 Codex 信任确认，启动器必须再次确认角色 Prompt 已投递；
- 部分环境不提供上下文使用率，只能使用压缩状态、重复提问等行为信号判断 Leader 是否需要换届；
- Windows 子 Agent 的写入沙箱能力取决于具体 Codex/CC-Panes 版本；
- 当前只有 Codex + CC-Panes 参考适配器。

## 路线图

- 增加端到端场景测试；
- 抽取稳定的 adapter capability 接口；
- 增加 Claude Code 与其他 CLI 适配器；
- 支持更多终端编排器；
- 完善工作区恢复、失败注入和兼容性矩阵。

## 许可证

[MIT](LICENSE)
