# Codex History Unify

解决 Codex 桌面版在 DeepSeek 与 OpenAI/Codex 账号之间切换后，侧边栏本地会话“消失”的问题。会话数据仍在本地，只是被 `model_provider` 过滤隐藏了。

## 功能

- 将本地会话统一到当前 provider（`openai` / `custom`）
- 自动备份 `state_5.sqlite`、`codex-dev.db` 和 rollout 文件
- 支持 `--restore` 回滚
- 兼容 CC Switch 的“统一 Codex 会话历史”
- 支持 macOS / Windows

## 安装

macOS：

```bash
cp -R codex-history-unify ~/.codex/skills/
```

Windows：复制文件夹到 `%USERPROFILE%\.codex\skills\codex-history-unify`，需要 Python 3.11+。

## 自动模式（推荐）

安装一次后台守护（macOS LaunchAgent），之后不再需要手动修复：

```bash
python3 scripts/install_switch_guard.py --dry-run
python3 scripts/install_switch_guard.py --apply
```

之后流程固定为：关闭 Codex → 在 CC Switch 切换模型 → 打开 Codex。守护进程会在
后台完成会话 ID、reasoning、provider 标签、DeepSeek 原生 Responses 与模型目录
`text`/`image` 的校正，首次打开即可正常使用所有未归档任务。

只读检查命令：

```bash
python3 scripts/codex_switch_guard.py --plan-only
python3 scripts/codex_switch_preflight.py --dry-run
python3 scripts/install_switch_guard.py --uninstall
```

守护进程只在 Codex 刚启动（3 分钟内）且没有任务正在生成时才会自动重启一次；
长时间运行中的 Codex 永不被中断，待修复内容会在下次关闭时补齐。

## 手动使用

在 Codex 中说：使用 `$codex-history-unify` 刷新会话。

或手动执行：

```bash
python3 scripts/sync_provider.py --dry-run
python3 scripts/sync_provider.py --apply
```

回滚：

```bash
python3 scripts/sync_provider.py --restore
```

## 说明

- 切换账号后会话不会丢失，只是被 provider 过滤
- 所有修改前会自动备份到 `~/.codex/skill-backups/unify-codex-history/`
