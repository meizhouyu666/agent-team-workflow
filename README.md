# Agent Team Workflow

[English](README_EN.md)

一个用于 Codex + CC-Panes 的三角色开发工作流插件。

> 当前版本：**0.2.0 alpha**。默认团队是 Codex Leader、Codex Executor Mother 和 Codex Independent Reviewer。Claude Leader 适配已包含，但只会在用户明确选择 `claude-leader` 后启用。

## 主要行为

- `lean` 是默认模式：简短规格、针对性测试、一次审查，最多一次聚焦复审。
- 安装和升级不会自动创建窗格、迁移角色、修改权限或接管现有会话。
- 需求、计划和审查状态保存在项目内的 `.codex/` 文件中，便于中断后恢复。

## 安装

~~~powershell
codex plugin marketplace add meizhouyu666/agent-team-workflow
codex plugin add agent-team-workflow@agent-team-workflow
~~~

安装或升级后，请新建一个 Codex 会话以加载新版 Skill。

## 使用

在 Leader 会话中输入：

~~~text
使用 $lead-agent-workflow 接管这个项目，采用默认 codex-three-pane 拓扑，
先和我讨论需求与架构，在我批准规格前不要开始实现。
~~~

也可以单独调用：

- `$orchestrate-agent-team`：执行已批准的规格。
- `$review-agent-work`：只读审查冻结的实现快照。

协议和适配细节见 [docs/protocol.md](docs/protocol.md) 与 [docs/operator-guide.md](docs/operator-guide.md)。

## 许可证

[MIT](LICENSE)
