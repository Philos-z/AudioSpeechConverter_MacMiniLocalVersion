# 按任务冻结编辑策略

`strict_v1` 保持原有事实与数量检查。`fluency_first_v1` 将目标改为中文自然通顺，允许意译、简化、专名取舍和意义偏差，由 Proxy 实际编辑并独立二审。源文、源ID、人物、时间和原始证据保持绑定；流畅策略的批准不能复用到严格任务，严格旧批准也不能静默改成流畅批准。

公开源码没有携带原安装的授权。缺少 `config/editorial_policy.json` 时，新任务仍采用严格模式。需要流畅模式时，先由本机使用者明确选择这一标准，将自己的决定保存为本机文件，例如 `config/editorial_authorization.local.json`，再复制 `config/editorial_policy.example.json` 为 `config/editorial_policy.json`，填写默认策略及本机授权文件的相对路径和实际SHA256。

流畅默认配置使用以下字段；空示例不构成授权：

```json
{
  "schema": "editorial_policy_defaults_v1",
  "default_policy": "fluency_first_v1",
  "decision_maker": "proxy",
  "ask_for_editorial_choices": false,
  "meaning_deviation_allowed": true,
  "authorization_ref": {
    "path": "config/editorial_authorization.local.json",
    "sha256": "填写本机授权文件的真实SHA256"
  }
}
```

授权文件及实际配置留在本机，路径不能越界或经过符号链接。授权文件变化、SHA不符或字段缺失时，创建任务会停止。初始化脚本不会自动启用该策略或配置定时制作。

新任务创建时从已核验默认值冻结策略。之后修改默认值只影响下一次创建；旧任务缺少字段仍为严格模式。正式分集通过父任务、源片、分集计划和ASR的完整绑定继承策略。已完成任务不会因此重做，明确暂缓、取消和原有调用预算不会撤销或重置。

声音风险采纳要求当前全部配音组的完整PCM、声音清单、无目标提示识别及机器检查证据一致，且无静音、损坏、截断或其他待修复客观故障。只有已完整检查的读音或识别不确定风险可以另记策略采纳；原 `needs_review` 保留，不改写为机器批准或人工试听，也不新增配音或恢复额度。

GPU串行、字幕真实声音定位、画面停格上限、空间余量、严格小于50,000,000字节、全媒体核验和归档要求继续适用。每日制作的独立授权及邮件账户配置见[每日制作说明](tagesschau_daily_automation.md)。
