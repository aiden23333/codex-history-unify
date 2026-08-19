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

## 使用

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
