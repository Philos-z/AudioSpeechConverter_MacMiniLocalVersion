# 本机部署说明

## 环境和源码

使用 Apple Silicon macOS 与 Python 3.11。递归获取两个固定提交的 Git 子模块，保留它们自己的 Git 元数据；MLX 运行时会核对上游提交和源码身份。

FFmpeg 必须带 libass、libx264 和 macOS 视频编码器。当前程序按 Homebrew `ffmpeg-full` 路径调用；不能以只有基础编解码器的版本替代。

`scripts/bootstrap_source.py` 默认只检查环境。`--init-config` 仅从示例创建尚不存在的普通配置。`--install-deps` 才会安装依赖；它不会下载模型、启动配音或读取真实文本服务凭证。

## 分开准备模型

仓库不包含下列权重或数据，请在本机按固定版本准备：

1. `models/indextts-2.5/`：原 IndexTTS-2.5 权重与 CAMPPlus、声码器等所需缓存。固定文件清单在 `reports/models.lock.json`。
2. `models/indextts-2.5-mlx-8bit/`：使用 MLX 子模块的转换工具，从上述同版权重离线转换；保留转换清单和运行时需要的所有文件。不能仅把原 Torch 文件改名放入此目录。
3. `models/faster-whisper-small/`：CPU 德语识别模型，路径与 `config/local.json` 一致。
4. `models/.torch/hub/checkpoints/955717e8-8726e21a.th`：Demucs htdemucs 权重，流水线使用固定 SHA 校验。
5. 逐句字幕对齐所需 Whisper/CTC 资源，以及 tokenizer/NLTK 等原模型依赖缓存。

各资源按上游说明和许可获取。项目自带 `scripts/download_models.py` 可供核对原模型获取方式；本次同步没有重新下载、转换或打包任何权重。网页子进程启用离线缓存设置，启动前应确认本机资源已经齐备。

## 私有文本服务

通用调用适配器使用 `config/web_proxy.json` 中的 `base_url`、`api_key`、`model`。只在本机从空示例填写。真实 endpoint、凭证、账号与服务部署不属于此仓库。

文本服务承担翻译、口语化、独立复核与受限恢复建议。受限恢复只允许既定的数据动作，不授权修改任意代码、配置、声音或质量检查。

## 启停与局域网

```bash
envs/asr/bin/python -B web/service.py start --root "$PWD"
envs/asr/bin/python -B web/service.py status --root "$PWD"
envs/asr/bin/python -B web/service.py stop --root "$PWD"
```

局域网访问需要将 `PHILOS_LAN_HOST` 设置为本机实际地址，再启动服务。默认网址输出指向 `http://127.0.0.1:8766/`。服务仍限制 Host、Origin 和文件访问边界。

## 迁移后的验收

先运行源代码测试与环境检查，再用自己准备的短素材验证识别、人物参考、完整意群配音、字幕、背景和导出。确认小样后才扩展到长片。MP4 上限由实际字节数检查，失败不会被标记为已完成。

本仓库保存已锁定制作版本；新机器安装、不同画幅、不同人物或无人值守长片成功率需要在对应环境重新验证。
