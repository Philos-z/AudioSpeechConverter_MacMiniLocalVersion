# AudioSpeechConverter — Mac Mini Local Version

## 首次无用户干预成片（2026-11-08）

2026年11月8日，项目首次无需用户中途介入，完成一期节目的中文译配、双语字幕、媒体核验、正式归档及完成通知。节目为11月7日20:00 Tagesschau；成片13分57秒、47,788,168字节，覆盖161组和298个原始ASR源ID。[里程碑与验证范围](docs/FIRST_UNATTENDED_RUN.md)记录了实际结果和边界。

本次同步加入 `fluency_first_v1`：Proxy 自主决定中文意译、简化和专名表达，以自然通顺为准，允许意义偏差。固定控制器可基于完整声音证据采纳读音或识别不确定风险，保留原疑点，不记作人工试听或严格事实批准。策略在新任务创建时冻结；旧任务、明确暂缓、取消及既有预算保留。公开源码需按[编辑策略配置](docs/EDITORIAL_POLICY.md)准备本机授权，未配置时保持严格模式。

每日入口支持柏林当地21:00领取当天节目，5分钟续办只处理最新到期日已经领取的意图。旧期暂缓不会阻挡新日期，跨午夜保留节目日期，提交结果不明时先核对唯一任务。调度器与邮件账户仍须在本机单独配置，见[每日制作说明](docs/tagesschau_daily_automation.md)。

## 固定恢复与成片归档

上传后由本机控制器执行固定流程，Proxy 负责文字编辑和独立审校。已确认的临时网络故障按本地有限策略重试；翻译疑点按当前原片和逐行输入局部复核。异常声音使用相同原句、参考与正式 Torch25 参数重配，保留其他声音和旧版本；配音前预检双语字幕，导出后核验实际 AAC 响度。

完成的视频及配套音轨、字幕和检查报告自动复制到项目“成片”目录；主视频名为“中文双语成片.mp4”。归档核对全部 SHA，不同修订另存，旧成片保留。断点空间检查只扣除已核验且可复用的阶段，必要时清理当前任务白名单内的可再生背景临时文件。文字和媒体各自的持久预算、8GiB 默认空间余量及严格小于50MB的最终校验继续生效。

使用、限额及停止条件见[网站内恢复说明](docs/WEBSITE_RECOVERY.md)。机器检查与人工完整试听分别记录。

源码包含整稿独立审校、逐组完整声音内容核对、完整源文件获取、说话人参考检查及有界服务崩溃接续。正式25步、seed 42、独立背景、预算与最终媒体检查保持。每日发现和状态协调工具只读观察；独立的正式启动入口负责在本机明确授权后提交，仓库不包含账户连接器或已安装的调度服务。

实际素材的单期授权、译文、人物与声音选择不属于公开源码。相关模块以明确拒绝专项操作的适配器保留导入契约：普通新任务走通用流程，出现未发布的专项凭据时停止，不能静默忽略或把旧期例外外推。详见[专项边界说明](docs/PRIVATE_HOOKS.md)。

外语视频的本地中文译配工具。此仓库保存前端、后端与固定依赖版本，不包含实际 Proxy 服务、密钥、音视频素材、人物参考、任务数据、模型权重、缓存或 Python 环境。

## 当前行为

- 网页上传或公开链接导入，自动识别源语言或手动指定，使用 Proxy 按该语言翻译并独立二审。
- 长视频依据可靠的句间/词间停顿分集，每集最多30分钟；实际中文配音超时会缩短源分集重新验证。各集串行处理，完成后可选择分集预览与下载。
- 显示进度，短视频保留前两个完整意群的人声试听，支持取消、续跑和成果下载。
- 本机 CPU 识别与单个 Apple GPU 背景分离可并行；重模型与配音保持单进程执行。
- 文本翻译、口语化、第二遍审校与受限恢复使用独立配置的文本服务。仓库仅保留通用调用接口。
- 默认配音为 `hybrid-gpt-preserve-25-pad-eager`，25 步、seed 42、自然时长、参考情绪。保留完整句或意群、自然标点和真实字幕对齐。
- 当前网页渲染器支持 16:9 横屏，960×540、25fps；中文在上、原文在下。其他画幅需先适配并检查。
- 独立背景混音、Philos.club 深色衬线字与浅色半透明底框水印及可行时的全轨统一母带增益策略。
- MP4 必须严格小于 **50,000,000 字节**。超限时按时长预算进行 H.264 两遍压缩，目标 48MB，复制完整音轨并核对编码包；不截断文件或台词。
- 最终媒体经过时长、帧数、字幕、音量和完整解码检查。工程检查不替代新素材的内容和听感审核。

使用与实测限制见 [长视频与多语种说明](docs/LONG_SOURCE_MULTILINGUAL.md)。`locked-2026-10-21` 标签保留原锁定版本；当前主分支继续包含后续功能。旧CLI仍沿用德语入口。

## 获取完整源码

```bash
git clone --recurse-submodules https://github.com/Philos-z/AudioSpeechConverter_MacMiniLocalVersion.git
cd AudioSpeechConverter_MacMiniLocalVersion
```

两个后端依赖以 Git 子模块固定到原运行提交；不要用浮动的最新版覆盖：

| 子模块 | 固定提交 |
| --- | --- |
| `vendor/index-tts` | `d9e41aac89fd00b3d71497fddb287b7f24613712` |
| `vendor/mlx-indextts2-inference` | `a6d8fef49b3c36fa8bccaf5a780091e9b864f8e7` |

如果最初未递归克隆，执行 `git submodule update --init --recursive`。

## 本机准备

当前运行目标为 Apple Silicon macOS、Python 3.11、带 libass/libx264 的 FFmpeg。程序使用 Homebrew 的 `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` 与 `ffprobe`，以及 macOS 字体。其他系统需要适配，不能直接视为已验证。

```bash
brew install python@3.11 ffmpeg-full yt-dlp
python3.11 scripts/bootstrap_source.py --check
python3.11 scripts/bootstrap_source.py --init-config
```

初始化仅复制不含实际服务地址和密钥的通用设置；已有配置不会被覆盖。依赖安装是单独的显式步骤：

```bash
python3.11 scripts/bootstrap_source.py --install-deps
```

环境位置：

- `envs/asr/`：CPU 识别。
- `vendor/index-tts/.venv/`：Torch、Demucs、媒体处理与声码器桥接。
- `vendor/mlx-indextts2-inference/.venv/`：MLX 配音。

依赖约束位于 `requirements/` 与 `config/native_mlx_runtime.constraints.txt`。初始化脚本不会下载模型，也不会复制真实 Proxy 配置。

## 模型与私有设置

按照 [部署说明](docs/DEPLOYMENT.md) 单独准备模型。`reports/models.lock.json` 仅是原模型版本、文件大小及校验值清单，包含在仓库内以保留运行时身份检查；它不是模型权重。

将 `config/local.example.json` 复制为 `config/local.json` 后，设置本机 ASR 模型目录。文本服务如需启用，另将 `config/web_proxy.example.json` 复制为 `config/web_proxy.json`，仅在本机填写 `base_url`、`api_key`、`model`。真实文件被 Git 忽略。此仓库不提供或部署该服务；未配置时网页不会声称翻译已经就绪。

历史素材的专用修正注册表也不随源码发布。新任务使用通用路径；若有明确审核过的专项需求，见 [本地数据边界](docs/LOCAL_DATA.md)。

## 启动网页

```bash
envs/asr/bin/python -B web/service.py start --root "$PWD"
```

在本机打开 `http://127.0.0.1:8766/`，或双击 `启动局域网配音.command`。默认不安装登录自启。

要从其他局域网设备访问，启动前将 `PHILOS_LAN_HOST` 设置为这台机器的实际局域网地址；它用于请求 Host 校验。启动器和仓库没有绑定原机器的固定地址。

## 代码结构

```text
web/static/       网页界面
web/              HTTP服务、队列、翻译接口、编排、字幕和媒体适配
scripts/          识别、说话人、配音、背景、导出和验收工具
tests/            媒体与发布边界检查
config/           无凭证示例及运行时版本约束
requirements/     三套独立环境的补充版本约束
vendor/           固定提交的上游源码子模块
assets/           产品标志
reports/models.lock.json  固定模型身份清单
release/          本次源码锁定清单
```

## 验证

```bash
vendor/index-tts/.venv/bin/python -B -m unittest discover -s tests -v
python3.11 scripts/bootstrap_source.py --check
```

测试使用已准备的 Torch 媒体环境，其中包含 NumPy、SoundFile 和 Pillow；HTTP 检查会创建临时本机端口，字幕几何测试使用 macOS 字体。部分媒体测试需要上述 FFmpeg。新克隆目录的模型、环境和私有设置尚未准备时，不应把源码检查通过当作整条配音流程已部署完成。第三方源码与模型的来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
