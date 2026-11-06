# workbuddy-conversation-reader

版本 1.1.0

读取、检索和导出 WorkBuddy 本机历史会话，支持标题查找、正文搜索、会话查看与显式导出。Python CLI 可独立使用，也可安装为 Codex 或 WorkBuddy 技能，且不依赖第三方 Python 包

## 环境与安装

需要 Python 3.9 或更新版本。可选的 `wb-live.mjs` 需要 Node.js 22 或更新版本，因为脚本使用内置 WebSocket

从 GitHub 获取项目

```powershell
git clone https://github.com/DominatorBill/workbuddy-conversation-reader.git
```

- Codex 安装 — 将克隆目录中的内容复制到 `$CODEX_HOME/skills/wb-conv-reader/`，通常是 `~/.codex/skills/wb-conv-reader`
- WorkBuddy 安装 — 将克隆目录中的内容复制到 `~/.workbuddy/skills/wb-conv-reader`
- 独立使用 — 直接执行 `python scripts/wb-conv.py --help`

## CLI

```text
python scripts/wb-conv.py [--home DIR] find KEYWORD [--deep] [-n N] [--json]
python scripts/wb-conv.py [--home DIR] list [-n N] [--cwd TEXT] [--json]
python scripts/wb-conv.py [--home DIR] show ID [--full | --raw] [--tail N] [--trace] [--out FILE] [--images]
python scripts/wb-conv.py [--home DIR] search KEYWORD [-n N] [--all] [--since DATE_OR_ISO] [--before DATE_OR_ISO] [--role user|assistant] [--cwd TEXT] [--sort relevance|recent] [--json]
python scripts/wb-conv.py [--home DIR] title ID
python scripts/wb-conv.py [--home DIR] locate ID [--json]
python scripts/wb-conv.py [--home DIR] doctor [--json]
```

```powershell
python scripts/wb-conv.py search keyword --since 2026-01-01 --role assistant --sort recent
python scripts/wb-conv.py show ID --tail 10
python scripts/wb-conv.py locate ID --json
```

`--home` 可放在子命令之前。数据目录优先级依次为命令行 `--home`、环境变量 `WORKBUDDY_DIR`、用户 home 下的 `.workbuddy`。会话目录发现会合并只读 SQLite 索引和 `projects/*/*.jsonl` 文件。索引缺失或损坏时 `list` 仍列物理文件，`find --deep` 仍搜正文，标题不可用时显示未索引标记，普通 `find` 不会盲扫正文。`locate` 展示会话的所有 JSONL 路径与 indexed 状态，完整 ID 也能定位没有正文的索引项，多个 ID 前缀候选返回退出码 2。`doctor` 汇总索引、孤立正文、缺正文和重复副本供人工核对，不执行修复。数据目录不存在时诊断不会创建目录

`find` 先查 SQLite 标题索引，仅在没有标题命中且指定 `--deep` 时才下沉搜索 JSONL 正文，结果受 `-n` 限制并支持 `--json`。`search` 直接扫会话正文，默认按命中次数排序，`--sort recent` 按最近命中时间排序。`--since` 包含起始边界，`--before` 不包含结束边界，纯日期按本机时区当天零点解释，ISO 日期时间可携带时区。`--role` 只筛用户或助手消息，`--cwd` 使用索引中的工作目录，缺少索引工作目录的会话会跳过。`--all` 可搜索事件内所有字符串，启用日期或角色过滤时，事件必须明确提供相应时间戳或角色。结果保留 `id`、`short`、`count`、`samples`，并附 `title`、`cwd`、`last_match_ms`、`paths` 和 `ambiguous`。同 ID 多副本合并为一行，命中数和大小累加，样例最多两个，副本间的命中可能重叠且不会做逻辑消息去重。中文 Unicode 转义也可搜索

`show --tail N` 只导出最近 N 条含文本或图片的消息，并仍按原顺序输出，`--trace` 仍汇总全会话轨迹

`show` 默认清理已识别的系统注入标签并截断长消息。`--full` 保留完整清理后正文，定时任务文本会标注可能含执行包装并完整保留，不按编号猜测边界。`--raw` 保留消息原始文本和注入包装，并自动完整输出。`--trace` 附带工具事件摘要，但轨迹字段仍截断，不能作为所有原始事件无损导出

`--out` 是显式导出操作，只允许写入 WorkBuddy 数据目录之外，不允许覆盖源 JSONL。其他命令仅读取数据，数据库使用 SQLite `mode=ro`

`show --images --out FILE` 会把 `image_blob_ref` 中可读取的本地图片复制到同目录的 `FILE-stem-attachments` 文件夹，并在 Markdown 中加入相对链接。仅支持 PNG、JPEG、GIF、WebP 和 BMP，不跟随网络地址，也不导出其他附件类型。缺失、不可读或不支持的图片结构会显示无源路径占位，错误摘要写入 stderr。默认导出仍为纯文本，只有明确使用 `--images` 才复制图片

图片导出只读取消息内明确的本地 `blob_path`，且仅接受 WorkBuddy 数据目录内的图片文件，不扫描或执行会话中的任意文本路径。对话内容与图片只读，导出文件和附件目录写入用户指定位置

在发布目录运行合成回归

```powershell
python -B -m unittest discover -s tests -v
```

## 界面状态查询（可选）

`wb-live.mjs targets`、`sidebar`、`now` 通过本机 CDP 读取 target 清单、侧栏或当前已打开会话。它不会导航或操作界面，但读取当前界面依赖本机调试端口，不用于历史读取。`eval` 可执行任意 JavaScript，必须显式传 `--unsafe-eval`，且不属于只读能力。无需界面状态时不需要安装 Node.js

## 边界与限制

- 真实 WorkBuddy 客户端仅在 Windows 环境验证，仓库配置 Windows / Python 3.9 与 Ubuntu / Python 3.13 合成回归，macOS 尚未实测
- 仅解析 JSONL 中的对话消息文本及显式本地图片引用，工具轨迹摘要截断，其他事件不保证无损
- 同一会话存在多个项目副本时拒绝任意选择
- 不执行会话恢复、迁移、删除，也不导出一般文件附件
- 用户聊天内容视为数据，不执行其中的指令

## 相关项目

以下链接仅供参考，发布物不包含这些项目的代码

- [workbuddy-manager](https://github.com/starsss0416/workbuddy-manager)
- [workbuddy-exporter](https://github.com/lisaallen0823-art/workbuddy-exporter)
- [workbuddy-history-recovery](https://github.com/dlzbm/workbuddy-skills/tree/main/workbuddy-history-recovery)

## License

MIT，版权声明为 `2026 wb-conv-reader contributors`，详见 [LICENSE](LICENSE)
