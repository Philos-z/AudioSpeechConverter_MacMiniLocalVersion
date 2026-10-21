# 源码和本地数据的边界

本仓库使用允许清单控制 Git 跟踪范围。实际文本服务配置、视频与音轨、字幕译稿、人物参考、模型、缓存、任务、运行状态和历史实测文件均留在本机。

可以提交的配置只有 `*.example.json` 和无凭证的运行时约束。`reports/models.lock.json` 是唯一允许跟踪的 reports 文件，仅保存模型身份校验信息。

## 专项修正

`picture_holds.py`、`resolve_native_export.py` 与 `review_compilation.py` 保留严格的专项审核与绑定校验。特定素材的任务编号、字幕行、媒体校验值和参考窗口不随源码发布，改从被忽略的 `config/source_scopes.local.json` 读取。

`config/source_scopes.example.json` 只含 schema，没有任何注册。缺少本地注册时，普通新任务继续走通用路径；试图使用专项修正凭据会被拒绝。创建注册并不替代原有用户选择、真实来源、媒体 SHA、完整音频与审核收据校验。

不要把本机配置、历史任务或人物参考复制进示例文件。提交前检查 `git status` 和实际暂存内容；不要对数据目录使用强制添加。
