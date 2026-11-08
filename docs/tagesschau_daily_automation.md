# 每日 Tagesschau 自动制作

每日入口在 Europe/Berlin 当地 **21:00** 领取当天最新常规 Tagesschau 的官方 SD 270p，并通过原本机 API 提交任务。随后由每 5 分钟的跟进入口续办已领取意图；制作服务保持原单进程重模型队列。夏令时和冬令时均按柏林当地时间计算。

源码包含发现、状态投影和提交入口，**不安装外部调度，也不连接邮件账户或自动发送邮件**。部署者需要单独配置调度和已授权的邮件连接器。空白示例配置没有提交授权；正式 `tagesschau_daily_start.py` 在配置及证据通过后会执行 `POST /api/jobs`。

## 本机配置与授权

将 `config/tagesschau_daily_automation.example.json` 复制为未跟踪的 `config/tagesschau_daily_automation.json`。保留 `root: "."` 或设置当前安装根目录，使用当前服务的 loopback API 和本机 state 路径。填写实际授权的通知收件人及外部调度标识；这些账户、标识和任务状态只保存在本机，不能提交到 Git。

固定策略为：

- `daily_local_time: "21:00"`、`timezone: "Europe/Berlin"`。
- `daily_submission_policy: "current_date_independent_v1"`：旧期失败、明确暂缓或冻结等待仅影响各自一期，不能阻挡新日期。
- `late_resume_policy: "latest_due_intent_only"`：跟进入口只处理最新到期日期的现有 intent，不领取新节目，不补跑更老积压。
- `old_holds_block_new_dates: false`，新任务预留 `new_job_reserve_gib: 8`。
- `source_language: "de"`：向服务明确提交德语来源；编辑策略由服务在创建任务时冻结，参见 [编辑策略](EDITORIAL_POLICY.md)。

`daily_current_date_authorization_ref` 必须指向当前项目内、由真实用户授权形成的 JSON 证据及其完整 SHA256。入口核验的授权结构包括：

```json
{
  "schema": "daily_current_programme_independent_start_authorization_v1",
  "effective_schedule": "21:00 Europe/Berlin daily",
  "old_hold_blocks_only_its_own_job": true,
  "old_job_hold_or_budgets_revoked": false,
  "finish_and_email_existing_authority": true
}
```

该结构用于保存既有授权，示例文本不能作为真实用户授权。证据还应保留用户实际决定的出处；授权路径和 SHA 由部署者写入本机配置。更改配置或授权证据会触发校验，不会重置任务恢复预算。

首次部署需建立本机队列文件 `web/state/tagesschau_daily_automation.json`：

```json
{
  "schema": "tagesschau_daily_automation_state_v1",
  "generation": 0,
  "daily_intents": {},
  "episodes": {},
  "watched": {},
  "notifications": {}
}
```

已有安装必须保留原队列、任务、hold、预算及收据。不要以空文件覆盖现有 state，也不要把本机证据或实际配置加入发布仓库。

## 每日与跟进调度

从项目根目录运行，使用已经配置的 Python 环境。以下 `UNIQUE` 必须在每次执行时替换为新的唯一后缀；证据目录不能复用。

每日 21:00 先执行 GET-only reconcile：

```sh
python -B web/tagesschau_daily_tick.py --root . --mode reconcile --evidence-dir reports/tagesschau_daily/tick_UNIQUE
```

再执行正式每日提交入口：

```sh
python -B web/tagesschau_daily_start.py --root . --mode daily --evidence-dir reports/tagesschau_daily/start_UNIQUE
```

每 5 分钟同样先 reconcile，再执行：

```sh
python -B web/tagesschau_daily_start.py --root . --mode resume --evidence-dir reports/tagesschau_daily/resume_UNIQUE
```

每天的领取仅由 `daily` 执行。最新到期日为当地 21:00 之后的今日，以及 21:00 之前的昨日。因此延迟到午夜的每日入口仍处理昨日节目；跟进入口也只续昨日已领取意图，不会提前领取今晚节目。若最新到期日没有 intent，`resume` 返回 `no_due_intent`，不会退回更老的冻结积压。已提交旧任务仍由其 watch 和原服务继续跟进。

同日 intent、episode 和 watch 只能绑定同一 job。明确取消、暂停和本期用户 hold 必须保留；旧日期的 hold 不构成新日期的全局阻挡。状态协调只更新派生投影及摘要；投影中的 `daily_start_candidate` 本身没有外部执行权限。

外部每日调度应保持启用，并在领取新意图时激活跟进调度。只有所有 watch 均完成、邮件均已证实发送、没有待办或提交不明 intent，且重读 generation 未变化时，才可暂停跟进调度。正常状态未变时保持安静，只在完成、真实失败或必须采取行动时通知。

## 官方节目与一次提交

只按官方 metadata 的 `seriesTitle`、常规时刻标题和 `broadcastedOnDateTime` 选择最新且已播出的常规 Tagesschau，排除 100 秒、简明语言、手语、直播及其他节目。源必须是该期明确的 `download@web` → `SD 270p`，并与官方 HTTPS CDN、MP4 和 480×270 stream 相符。不能猜 URL 或退回旧期、高清版本。

所选节目必须属于 intent 日期。午夜续昨日时，发现截止时间不会超出昨日结束。SD270 尚未发布时保存候选 episode key，随后只等待这一期的下载地址；节目从首页消失也不自动改领另一节目。正式控制器下载后仍须核来源收据、SHA、480×270 和完整音视频解码。

提交前会核官方 HEAD、实际空闲空间（8 GiB 预留加源文件及余量）、本机服务项目身份及原队列容量。最多三个 running/queued 任务；满时保存 `waiting_queue`，不自行另起模型。所有网络调用在短队列锁之外进行，写 state 使用锁、generation 和 intent CAS。

入口先检查本机任务文件，按完整 source URL 和冻结 title 核唯一任务，找到则接管。POST 前持久化完整请求、请求 SHA、开始时间和唯一尝试；提交成功后再绑定 job、episode、watch 以及单一待发邮件 outbox。

**超时、中断或崩溃后不能盲重 POST。** 已保留提交尝试的 intent 只按本机完整 URL、title 和创建时间核唯一 job；公开 API 列表缺少 source URL，不能单凭列表证明可重发。无唯一证据时保持 `submission_unknown`，多匹配或来源冲突则阻止执行。即使崩溃发生在发送保留之后、实际 socket 之前，也不会自动消费第二次 POST。

## 制作、完成和唯一通知

跟进入口不运行模型、取消、retry/recover 或邮件连接器。健康任务由原控制器继续；故障仅走项目正式有界恢复入口，不重置文字、整稿、媒体或服务接管额度。机器检查、策略风险采纳和人工试听须如实区分。

完成通知必须晚于实际最终媒体核验和正式归档。要求完整语音内容检查、双语字幕全文及真实声音定位、时间轴和 EDL、实际 AAC/峰值、最终 MP4 全解码、每个 MP4 严格小于 **50,000,000 字节**，以及全部归档副本 SHA 一致。局部探针、仅译稿或仅配音完成不能记为最终成片。

邮件由另行连接并获用户授权的邮件连接器处理；每个 job 仅在 `watched[job_id].email` 管理一个 outbox，不在 intent、episode 和 watch 各发一次。发送前持久化精确收件人、主题、正文及其 SHA、job 与媒体 SHA，并标记 sending。发送一次后保存真实工具回执；结果不明时先从 Sent Items 核精确收件人、主题、完整正文及时间，不能在每次跟进时重发。没有发送或未能确认时保持 pending/unknown，不假记 sent。

源视频、原文、字幕、音频、账本、授权和邮件证据保存在本机。公开仓库只发布通用源代码与空白示例，不包含任何实际成片、账号或任务证据。

## 离线边界验证

```sh
python -B -m unittest discover -s tests -p 'test_tagesschau_daily_*.py'
```

测试使用临时项目、模拟官方 HTML 和本机 API，不接触生产 state、不启动模型或服务、不访问真实网络、不发邮件。覆盖当地 21:00、夏冬令时、午夜、旧期 hold、队列与空间、唯一提交、崩溃和超时后核对、冻结源/请求证据以及只读投影。
