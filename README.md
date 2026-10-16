# Siigo2api

Siigo2api 0.4.0 是 Windows 本地 Siigo API 桥接程序。它向 OMP 等客户端提供原生 Responses 和 Chat Completions 接口，保留各模型的协议、推理参数和工具调用格式，不做跨协议转换。OMP 工具在本机执行；桥接程序不把工具执行搬到云端。

服务仅监听 `127.0.0.1`，默认端口 `8766`。登录凭据和本地 bearer key 使用当前 Windows 用户的 DPAPI 加密，保存在 `%LOCALAPPDATA%\SiigoApiBridge`，不放进仓库。不要把端口转发到公网，也不要共享凭据文件或 `key` 命令输出。

只使用本人有权访问的账号、云机器和模型，并遵守 Siigo 网站条款及上游服务要求。程序使用的 Siigo 内部接口可能变化；账号权限、机器状态、配额和网站计费仍然适用。

## 安装与首次登录

需要 Windows 和 Python 3.11 或更高版本。普通运行无需在本机安装 Node.js，worker 在云端执行。先把源码放到自己选择的目录，在该目录打开 PowerShell：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
```

首次登录需要自己的 Siigo 云机器 ID。打开 Siigo 网站的机器页面，在对应机器详情页或页面 URL 中取得 24 位十六进制机器 ID。不要使用他人的 ID，也不要把 ID 或授权链接提交到仓库。将下面的占位符替换为自己的 ID：

```powershell
.venv\Scripts\python.exe -m siigo_bridge login --machine <YOUR_24_HEX_MACHINE_ID>
```

`<YOUR_24_HEX_MACHINE_ID>` 是说明用占位符，不能原样执行。命令显示官方授权链接和二维码；在自己的设备上确认授权后，程序保存加密凭据。`--machine` 为必填项，没有个人机器默认值。已有当前用户 DPAPI 凭据时，移动源码目录后可继续使用，无需重新登录。

查看本地凭据状态：

```powershell
.venv\Scripts\python.exe -m siigo_bridge status
```

`status` 只读取本地状态，不刷新云端 token，不打印密钥。DPAPI 存储绑定当前 Windows 用户，不要改用 SYSTEM、其他用户或管理员账号来绕开解密问题。

## 手动启动

必须先完成虚拟环境安装。启动器不会自动安装依赖、联网或发起登录。可双击根目录的 `start.cmd`，也可在 PowerShell 中运行：

```powershell
.\start.cmd
.\start.cmd -Port 8767
```

默认前台运行，输出保留在窗口中，按 Ctrl-C 停止。端口被占用时启动失败并返回非零退出码；程序不会杀掉其他进程或自动清空端口。换用空闲端口后，客户端的 `baseUrl` 也要相应修改。

双击后窗口若立即关闭，请在 PowerShell 中运行 `.\start.cmd` 查看错误；启动器失败时直接返回退出码，不会暂停并阻塞命令行调用。

脚本的完整接口为 `scripts/start.ps1 [-Port 8766] [-LogToFile]`，端口范围为 1–65535。例如：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Port 8766 -LogToFile
```

`-ExecutionPolicy Bypass` 仅作用于本次 PowerShell 进程，不永久修改系统执行策略。`-LogToFile` 将标准输出和错误分别写入 `%LOCALAPPDATA%\SiigoApiBridge\logs\bridge.stdout.log`、`bridge.stderr.log`，每次启动覆盖旧日志。日志留在本机，不应上传。

启动后可从另一窗口检查健康接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8766/health
```

`/health` 返回服务状态；它不代表账号已取得每个模型的调用权限。模型接口为 `/v1/models`、`/v1/responses` 和 `/v1/chat/completions`，调用时使用本地 bearer key。

## 接入 OMP

[examples/models.yml](examples/models.yml) 包含 `providers.siigo-local` 和全部 13 个模型配置。只把这个 provider 条目合并到自己的 `models.yml` 的 `providers` 下，保留其他 provider、默认模型和个人设置，不要覆盖整个配置文件。

示例通过命令读取当前用户 DPAPI 中的本地 bearer key：

```yaml
providers:
  siigo-local:
    baseUrl: http://127.0.0.1:8766/v1
    auth: apiKey
    authHeader: true
    apiKey: "!C:/Siigo2api/.venv/Scripts/python.exe -m siigo_bridge key"
    # models: 使用 examples/models.yml 中的完整列表
```

把 `C:/Siigo2api` 改为自己的源码目录。这里的 `apiKey` 是 OMP 命令型密钥解析配置，`key` 命令仅输出本地服务密钥；不要手工把输出粘贴到 YAML 或仓库。路径包含空格时，命令内的可执行文件路径也要加引号，并按 YAML 规则转义，例如：

```yaml
apiKey: '!"C:/My Projects/Siigo2api/.venv/Scripts/python.exe" -m siigo_bridge key'
```

### 模型预算与权限

示例保留现有 13 个 profile 的 `reasoning`、推理档位及映射、输入类型、兼容选项、上下文预算和输出预算。`contextWindow` 按厂商预算配置：GPT 系列为 1,050,000，Qwen、GLM 和 Claude 为 1,000,000，DeepSeek 为 1,048,576。它们不能证明 Siigo 网关实际支持同等容量。Siigo 当前声明的上下文为 272,000，尚未完成容量实测；全部 profile 的 `maxTokens: 128000` 按原配置保留，也不表示已验证可以生成这么多 token。

`catalog` 可查看模型目录中的厂商预算和网站声明：

```powershell
.venv\Scripts\python.exe -m siigo_bridge catalog
```

目录中列出模型不代表账号有权限调用，也不验证上游真实模型身份。当前账号的 Qwen Flash 经云机接口返回 `403 AccessDenied.Unpurchased`，用户也反馈网页端无法使用；应先核对上游模型开通权限或联系网站支持，不能把反复本地登录当作修复办法。示例 `cost` 中的零值仅为未校准的 OMP 计费占位；网站计费单位未换算成美元，不表示免费。

## 可选：当前用户登录后启动

默认不启用。安装前先确保手动启动可用，再明确执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\autostart.ps1 -Action Install -Port 8766 -WhatIf
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\autostart.ps1 -Action Install -Port 8766
```

接口为 `scripts/autostart.ps1 -Action Install|Uninstall [-Port 8766] [-WhatIf]`，省略 `-Action` 时为 `Install`。`-WhatIf` 只预览，不创建目录或快捷方式。无需管理员权限。脚本在当前用户的 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` 下创建 `Siigo2api.lnk`，登录后隐藏窗口运行 `start.ps1 -LogToFile -Port <端口>`，日志位置与上文相同。快捷方式不含账号密码或 token。

这是当前用户登录后启动，不是登录前的系统启动服务；不能以 SYSTEM 身份读取当前用户 DPAPI。已有同名但不属于当前源码目录的快捷方式时，脚本会拒绝覆盖或删除。

卸载配置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\autostart.ps1 -Action Uninstall -WhatIf
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\autostart.ps1 -Action Uninstall
```

卸载将本仓库拥有的快捷方式送入回收站；快捷方式不存在时不做修改。卸载不会停止已经运行的服务。移动或重命名源码目录前，先从旧目录卸载启动配置；在新目录确认可运行后，再自行决定是否重新安装。

## 开发

开发环境需要 Python 3.11+ 和 Node.js 22+。Node.js 用于隔离的 worker 测试，正常桥接运行不依赖本地 Node.js。

```powershell
.venv\Scripts\python.exe -m pip install -e ".[test]"
.venv\Scripts\python.exe -m pytest -q
```

GitHub Actions 在 `windows-latest` 上使用 Python 3.11、3.14 和 Node.js 22，安装 `.[test]` 后执行 pytest。工作流仅在 push / pull request 时运行，仅授予仓库内容读取权限，不发布包、不部署，也不读取账号 secrets。远程 CI 是否通过以实际运行结果为准。

## 提交与许可

仓库只应包含源码、测试、脚本和脱敏示例。不要上传 `.venv`、`.env`、`.env.*`、DPAPI 文件、密钥、凭据、日志、私人 OMP 配置或其他本地数据。`.gitignore` 已覆盖常见运行产物；发布前仍须人工检查待提交文件。

尚未选择开源许可证，当前不附带 LICENSE，也不授予额外使用、复制、修改或分发权利。正式公开发布前应先确定许可证，并单独核对第三方依赖及服务条款。
