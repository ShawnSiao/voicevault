# VoiceVault（声迹）

[English](README.md)

VoiceVault（声迹）是一个本地优先、单用户使用的公开内容归档与循证问答工具。系统以「人物」作为知识库边界，归属其平台账号与帖子版本；问答结果附带引用证据和不确定性说明。

本仓库发布软件，不发布公开人物语料。全新的人物归档工作区从空状态开始；帖子、索引、证据、浏览器会话与认证信息仅保留在本机。`voicevault init` 会创建本地结构，并提供用于体验兼容功能的合成示例数据。

## 已有功能

- 创建人物，并为同一人物绑定一个或多个平台账号。
- 创建本地采集任务，支持时间范围、覆盖检查、进度、恢复操作和 30 分钟任务租约。
- 保存帖子、版本、时间、来源 URL、采集观察记录和可审阅证据。
- 导入已完成的历史雪球 JSON 归档；导入前校验内容、去重，并绑定到既有人物账号。
- 构建人物范围内的知识库索引。默认使用本地全文检索；配置 embedding 服务后可使用向量检索。
- 对选定人物知识库提问，查看回答状态、引用证据和不确定性说明。
- 提供本地 HTTP 界面、JSON API 和命令行工具。
- 保留既有的 Role/Statement 与发布分析命令，作为兼容功能。

## 环境要求

- Git
- Python 3.11 或更高版本
- 仅在安装 Python 依赖，或访问已获授权的采集来源时需要网络连接

## Windows 安装与启动

在 PowerShell 中执行：

```powershell
git clone https://github.com/ShawnSiao/voicevault.git
Set-Location voicevault

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -e .

$root = (Get-Location).Path
$kb = Join-Path $root 'knowledge-base'
$env:VOICEVAULT_DATA_DIR = Join-Path $env:LOCALAPPDATA 'VoiceVault'

voicevault init --kb $kb
voicevault serve --kb $kb --root $root --data-dir $env:VOICEVAULT_DATA_DIR
```

如果 PowerShell 执行策略阻止激活虚拟环境，可以直接调用虚拟环境中的可执行文件：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\voicevault.exe --version
```

## macOS 安装与启动

在「终端」中执行：

```bash
git clone https://github.com/ShawnSiao/voicevault.git
cd voicevault

python3.11 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e .

ROOT="$(pwd)"
KB="$ROOT/knowledge-base"
export VOICEVAULT_DATA_DIR="$HOME/Library/Application Support/VoiceVault"

voicevault init --kb "$KB"
voicevault serve --kb "$KB" --root "$ROOT" --data-dir "$VOICEVAULT_DATA_DIR"
```

如果系统中的 `python3` 已是 Python 3.11 或更高版本，可以将 `python3.11` 替换为 `python3`。

## 打开本地服务

服务默认绑定到 `127.0.0.1`，启动后会输出访问地址。默认地址为：

```text
http://127.0.0.1:8765/
```

首次访问时人物归档工作区为空；初始化后的兼容知识库还包含合成示例数据。人物归档的推荐顺序如下：

```text
创建人物 → 绑定账号 → 创建采集任务或导入归档
→ 构建知识库 → 提交问题 → 查看证据
```

系统不会读取浏览器 Cookie，也不会自行访问外部平台。采集通过本地任务明确发起，并应遵守来源平台条款与适用法律。

## 查看服务能力

Windows PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/status
Invoke-RestMethod http://127.0.0.1:8765/api/capabilities
Invoke-RestMethod http://127.0.0.1:8765/api/workspace
```

macOS「终端」：

```bash
curl http://127.0.0.1:8765/api/status
curl http://127.0.0.1:8765/api/capabilities
curl http://127.0.0.1:8765/api/workspace
```

查看命令行能力：

```bash
voicevault --help
voicevault archive import --help
voicevault collection --help
voicevault question --help
```

## 复用已有本地运行数据

通过 `--data-dir` 指向已有的运行数据目录，不要将数据库复制到仓库内。例如 Windows：

```powershell
$env:VOICEVAULT_DATA_DIR = 'W:\VoiceVault'
voicevault serve --kb $kb --root $root --data-dir $env:VOICEVAULT_DATA_DIR
```

`--kb` 指向知识库目录，`--data-dir` 指向运行数据目录。两者都属于本地状态，不应提交到版本控制。

## 开发与验证

Windows PowerShell：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m unittest discover -s tests -t . -v
```

macOS「终端」：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -t . -v
```

## 数据与安全边界

导入内容前请阅读[数据政策](docs/DATA-POLICY.md)。不要提交已导入帖子、账号归档、索引、证据、导出物、截图、浏览器配置文件、Cookie、凭据或任务交接材料。

安全问题请按 [SECURITY.md](SECURITY.md) 提交；贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

本仓库中的原创代码采用 [MIT License](LICENSE) 发布。该许可证不授予第三方内容、平台数据、商标或用户提交内容的任何权利。
