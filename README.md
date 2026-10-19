# 闻记 · Wenji

在 Mac 上插入录音卡，录音会自动同步到家里的服务器，在服务器上完成转写、纪要和脑图。处理结果可以在网页上查看，其他服务也可以通过 MCP 接口查询。

```text
MacBook（uTools 插件）                              Mac Studio（Docker）
┌─────────────────────────┐   内网穿透 HTTPS   ┌──────────────────────────────────────┐
│ 发现 /Volumes/*/record  │ ─────────────────▶ │ server：比对 / 续传 / 任务队列 / 网页 / MCP │
│ 与服务端比对，已有的跳过 │                    │   ├─ asr-worker：本地 FunASR（可选）   │
│ 8MB 分片续传缺失文件    │                    │   ├─ 豆包 / 硅基流动等远端转写         │
└─────────────────────────┘                    │   └─ DeepSeek / 千问 / 豆包 / Ollama 等纪要 │
                                               └──────────────────────────────────────┘
```

| 目录 | 内容 |
| --- | --- |
| `server/` | FastAPI + SQLite 服务端：同步接口、处理流水线、网页、MCP |
| `asr-worker/` | 本地转写服务：FunASR Paraformer，支持说话人分离和热词，只在 Docker 内网访问 |
| `utools-plugin/` | uTools 插件：监测录音卡并同步，也可以选中音频文件手动上传 |

## 一、部署服务端（Mac Studio）

需要先安装 Docker Desktop。

```bash
cd wenji-cloud
cp .env.example .env
openssl rand -hex 32          # 生成令牌，填入 .env 的 WENJI_TOKEN
```

只使用远端模型时：

```bash
docker compose up -d --build
```

还要使用本地转写时，加上 `local-asr`：

```bash
docker compose --profile local-asr up -d --build
```

- 服务只监听 `127.0.0.1:8080`，需要通过内网穿透才能从外部访问。
- 数据保存在 `./data`（数据库、原始音频、播放文件），模型缓存在 `./models`。备份时复制这两个目录即可。
- 本地转写第一次运行会下载约 1GB 模型。在 Docker 里只能用 CPU（Docker 在 macOS 上无法使用 Apple GPU）。在 M1 Max 上原生运行实测，一段 4 分 45 秒的真实录音用时约 3.5 分钟（约为录音时长的 0.7 倍），按这个速度 1 小时的录音大约需要 45 分钟，容器里会更慢一些。建议在 Docker Desktop 中给虚拟机分配至少 8GB 内存。

### 内网穿透

把 frpc、cloudflared 等工具的转发目标设为 `127.0.0.1:8080`，**必须使用 HTTPS**，因为令牌和录音都会经过这条链路。

- 插件每个分片 8MB，能通过 Cloudflare 等服务常见的 100MB 请求体限制。
- 服务端已开启 `--proxy-headers`，在 HTTPS 下登录 Cookie 会带上 Secure 标记。

## 二、配置模型

打开网页（穿透后的地址），用令牌登录，进入“设置”。每项服务都有“测试连接”按钮，会先保存再检测。

**处理方式**：默认新录音上传后自动转写。录音很多、只想挑着处理时，关闭“新录音自动转写”，新录音会保持“待处理”，在详情页点“开始转写”。

**转写**

| 服务 | 说明 |
| --- | --- |
| 本地 FunASR | 需要以 `local-asr` 启动。Paraformer 中文模型支持说话人分离和热词；SenseVoice 支持多语种 |
| 字节豆包录音文件识别 | 支持区分说话人。需要在火山引擎开通“录音文件识别大模型”，然后填写 API Key 或 App ID + Access Token |
| OpenAI 兼容接口 | 预置了硅基流动 SenseVoice、OpenAI、Groq。这类接口不区分说话人；录音会自动切成 10 分钟一段上传 |

**纪要与脑图（OpenAI 兼容接口）**

已预置 DeepSeek、阿里百炼（通义千问）、字节火山方舟（豆包）、小米 MiMo、Kimi、智谱、硅基流动，以及本机 Ollama 和 LM Studio。选好厂商后点“获取 API Key ↗”打开该厂商控制台，填好 API Key 后点模型旁的“获取列表”，会调用厂商的 `/models` 接口列出可用模型供选择（转写只列语音模型）；不提供该接口的厂商可手动填写模型名，**以各厂商控制台显示的为准**。

- 想用本地大模型：在 Mac Studio 上直接运行 Ollama（不放进 Docker，这样能用 GPU），服务地址填 `http://host.docker.internal:11434/v1`。
- 没有配置纪要模型时只做转写，状态照常显示“已完成”。
- 纪要的提示词可以自定义；脑图是生成纪要后再调用一次模型，输出 Markdown 大纲。
- 生成纪要后会再根据纪要起一个标题（如“三季度产品复盘会”），替换卡上的默认文件名；手动改过的标题不会被覆盖。之前已完成的录音，点“生成纪要 / 重新生成纪要”即可补上标题。

**热词与纠错**：热词每行一个词，只对本地 Paraformer 生效。纠错规则是“错词=正确词”，按原文直接替换，对所有转写服务都生效。

## 三、安装 uTools 插件（macOS / Windows）

1. 在 uTools 插件市场安装“uTools 开发者工具”。打开后选择“新建项目”，导入 `utools-plugin/plugin.json`，然后点击运行。
2. 在 uTools 中输入“闻记同步”打开插件，展开“设置”，填写服务地址（例如 `https://wenji.example.com`）和令牌，点击“测试连接”。
3. 想让插件常驻后台、插卡后自动同步：打开插件右上角菜单 →“插件应用设置”，开启“跟随主程序同时启动运行”，关闭“退出到后台立即结束运行”。

插件界面分“同步 / 记录 / 设置”三页：顶部显示与服务端的连接状态；插卡后列出卡上每条录音的状态（已同步 / 待上传 / 上传中 / 失败），同步时显示总进度、速度和剩余时间。

同步规则：

- 每 3 秒检查一次可移动卷：macOS 看 `/Volumes`，Windows 看系统盘以外的 D:–Z: 盘符。含 `record/` 目录的卷视为录音卡，新插入的卡会自动同步。
- **先和服务端比对“文件名 + 大小”，服务端已有的直接跳过**，不需要读取文件内容。中途拔卡或断网时，已经传上去的部分会保留，下次从断点继续。
- 卡上同一段录音会同时有 `.opus` 和 `.wav` 两份，默认只传 `.opus`（体积约为 `.wav` 的 1/16）。设置里可以改为传 `.wav`。
- 在访达（Windows 为资源管理器）中选中音频文件，呼出 uTools，选择“上传到闻记”，即可手动上传。
- 可以选择同步成功后自动推出录音卡（macOS 用 `diskutil`，Windows 调用资源管理器的“弹出”）。
- 访问令牌优先保存在 uTools 的加密存储中。
- 可以保存多个服务地址（例如家里的内网地址和穿透后的 HTTPS 地址），在设置里点击切换；当前地址连不上时会自动尝试其他已保存的地址。

## 四、网页功能

- 录音按日期分组，可按状态筛选，全文搜索标题、转写和纪要，命中处高亮。
- 详情页：纪要、脑图、转写、说话人四个标签。播放时转写跟随高亮当前句，点时间戳跳转，支持倍速。
- 给说话人命名、修改标题、重新转写、重新生成纪要、导出 Markdown、删除。
- 可直接在网页上传音频（手机上也可以），与插件使用同一套续传接口。
- 适配电脑、平板和手机，跟随系统深色模式。
- “接入”页列出 MCP 地址、工具说明、Claude Code / JSON 配置示例、curl 自测命令，以及插件和 HTTP 接口说明，地址按当前访问域名自动生成。

## 五、MCP 接口

地址是 `https://<你的域名>/mcp`，传输方式为 Streamable HTTP，鉴权请求头为 `Authorization: Bearer <WENJI_TOKEN>`。

| 工具 | 作用 |
| --- | --- |
| `list_recordings` | 按关键词或日期列出录音 |
| `search_transcripts` | 全文搜索转写内容，返回命中的句子、时间点和说话人 |
| `get_recording` | 获取录音的元信息和处理状态 |
| `get_transcript` | 获取完整转写，已替换成命名后的说话人 |
| `get_summary` | 获取纪要（Markdown） |
| `get_mindmap` | 获取脑图大纲（Markdown，可以直接交给 markmap 渲染） |

以 Claude Code 为例：

```bash
claude mcp add --transport http wenji https://wenji.example.com/mcp \
  --header "Authorization: Bearer <WENJI_TOKEN>"
```

所有工具都是只读的。

## 六、安全与数据

- 网页、API 和 MCP 共用一个令牌，长度不少于 24 位，否则服务拒绝启动。网页登录后令牌保存在 HttpOnly、SameSite=Strict 的 Cookie 中。
- 设置页面不会回显 API Key，只显示“已保存”。
- 使用远端转写时会上传音频，使用远端纪要时会上传转写文字，请确认内容适合交给所选服务商。只用本地 FunASR 和本机 Ollama 的话，数据不会离开 Mac Studio。
- 在网页上删除录音后，下次插卡同步时，卡上的同一段录音会被重新上传。

## 七、开发与测试

```bash
# 服务端（转写和模型调用都用模拟数据，不调用收费服务）
cd server && pip install -r requirements.txt -r requirements-dev.txt && pytest -q

# 转写 worker（不需要加载模型）
cd asr-worker && pytest -q test_worker.py

# 插件
cd utools-plugin && node --test test/sync.test.js
```

## 已知限制

- 阿里云的语音识别接口需要提供公网可访问的音频地址，而服务端在内网穿透后面，暂未内置。阿里的文本模型（百炼）可以正常使用。
- 小米 MiMo、火山方舟等厂商的模型名更新很快，预置的名称只是示例，请以控制台为准。
- 卡上的 `.opus` 是设备写出的裸 Opus 数据，开头可能有几 KB 无法解码的内容，服务端会自动跳过（逻辑沿用听序）。如果仍然解码失败，可以在插件里改为上传 `.wav`。
- 脑图依赖 jsDelivr 加载 markmap；加载失败时会显示成大纲文本。
