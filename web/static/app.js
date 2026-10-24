"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    mode: "upload", file: null, busy: false, jobs: [], selectedId: null,
    selectedJob: null, sources: new Map(), listPending: false, detailPending: false, detailRevision: 0,
    healthPending: false, cancelPending: false, recoverPending: false, previewKey: "", toastTimer: null,
    recoveryDraftId: null, recoveryPolicyKey: "", recoveryDirty: false,
    reviewJobId: null, reviewRows: [], reviewLoaded: false, reviewLoading: false, reviewSaving: false,
  };
  const statusNames = {
    queued: "等待处理", running: "处理中", completed: "已生成预览",
    failed: "未完成", canceled: "已取消", cancelled: "已取消",
  };
  const stageNames = {
    queued: "等待处理", pending: "等待处理", created: "已接收素材",
    waiting: "等待处理", waiting_for_worker: "等待配音服务", awaiting_worker: "等待配音服务",
    worker_unavailable: "等待配音服务", preparing: "准备素材", uploading: "上传视频",
    uploaded: "已接收视频", downloading: "下载视频", download: "下载视频",
    extracting: "提取音频", extract_audio: "提取音频", audio_extract: "提取音频",
    separation: "分离人声与背景", separating: "分离人声与背景",
    transcribing: "识别原文", transcription: "识别原文", asr: "识别原文",
    translate: "翻译与调整表达", translating: "翻译与调整表达", translation: "翻译与调整表达",
    reference: "准备声音参考", voice_cloning: "准备声音参考",
    tts: "生成中文配音", synthesizing: "生成中文配音", dubbing: "生成中文配音",
    mixing: "合成配音与背景", mix: "合成配音与背景",
    subtitles: "制作双语字幕", subtitle: "制作双语字幕",
    rendering: "导出视频", render: "导出视频", exporting: "导出视频", encoding: "导出视频",
    completed: "审稿预览已生成", complete: "审稿预览已生成", done: "审稿预览已生成",
    failed: "处理未完成", canceled: "任务已取消", cancelled: "任务已取消",
  };

  class RequestFailure extends Error {
    constructor(status, message) {
      super(message || "Request failed");
      this.status = status;
    }
  }

  function create(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function displayError(error, fallback = "处理服务暂时遇到问题，请稍后重试。") {
    const code = Number(error?.status);
    if (code === 413) return "视频文件较大，暂时无法上传。请缩小文件后重试。";
    if (code === 404) return "暂时找不到这个任务，请刷新列表后再试。";
    if (code === 401 || code === 403) return "当前无法使用这项服务，请检查服务访问设置。";
    if (code === 429) return "当前处理任务较多，请稍后再试。";
    if (code === 502 || code === 503 || code === 504) return "处理服务暂未就绪，请稍后再试。";
    let message = typeof error === "string" ? error : error?.user_message || error?.message;
    if (typeof message !== "string" || !message.trim()) return fallback;
    if (/network|failed to fetch|load failed|connection|网络|连接失败/i.test(message)) return "暂时无法连接服务，请检查网络后重试。";
    if (/timeout|timed out|超时/i.test(message)) return "等待响应超时。任务可能已提交，请先刷新任务列表确认。";
    if (/llm|translation|翻译.*配置/i.test(message)) return "翻译服务尚未就绪，请稍后重试。";
    if (/worker|配音.*未连接/i.test(message)) return "配音服务尚未连接，请稍后重试。";
    // Backend errors can contain credentials or stack traces. Never expose them.
    if (/sk-[a-z0-9]|bearer|authorization|api.?key|token|secret|password|credential|traceback|exception|error:|python|ffmpeg|shell|pip|localhost|127\.0\.0\.1|https?:\/\/|\/Users\/|\/home\/|[a-z0-9_-]{24,}/i.test(message)) return fallback;
    if (/[\u3400-\u9fff]/.test(message)) return message.split("\n")[0].slice(0, 180);
    return fallback;
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), options.method === "POST" ? 60000 : 15000);
    try {
      const response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
      let data = null;
      const body = await response.text();
      if (body) {
        try { data = JSON.parse(body); } catch (_) { /* Do not display server HTML. */ }
      }
      if (!response.ok) {
        const detail = typeof data?.detail === "string" ? data.detail : data?.error?.message || data?.error || data?.message;
        throw new RequestFailure(response.status, typeof detail === "string" ? detail : "Request failed");
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new RequestFailure(0, "timeout");
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => { $("toast").hidden = true; }, 4500);
  }

  function announce(message) {
    if ($("live-status").textContent !== message) $("live-status").textContent = message;
  }

  function setMode(mode, focusTab = false) {
    state.mode = mode;
    for (const name of ["upload", "url"]) {
      const chosen = mode === name;
      $(name + "-tab").classList.toggle("active", chosen);
      $(name + "-tab").setAttribute("aria-selected", String(chosen));
      $(name + "-tab").tabIndex = chosen ? 0 : -1;
      $(name + "-panel").hidden = !chosen;
    }
    $("form-error").hidden = true;
    if (focusTab) $(mode + "-tab").focus();
  }

  function humanSize(bytes) {
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    if (bytes < 1024 ** 3) return (bytes / 1024 ** 2).toFixed(1) + " MB";
    return (bytes / 1024 ** 3).toFixed(2) + " GB";
  }

  function chooseFile(file) {
    if (!file) return;
    const looksLikeVideo = file.type.startsWith("video/") || /\.(mp4|mov|m4v|mkv|webm|avi|ts|mpeg|mpg|flv|wmv)$/i.test(file.name);
    if (!looksLikeVideo || file.size === 0) {
      formError(file.size === 0 ? "这个文件是空的，请选择有效的视频。" : "请选择一个视频文件。");
      return;
    }
    state.file = file;
    $("selected-name").textContent = file.name;
    $("selected-size").textContent = humanSize(file.size) + " · 本地视频";
    $("file-selected").hidden = false;
    $("drop-title").textContent = "视频已选好";
    $("drop-copy").textContent = "点击或拖入其他文件以更换";
    $("file-hint").hidden = true;
    $("form-error").hidden = true;
    announce("已选择视频：" + file.name);
  }

  function removeFile() {
    state.file = null;
    $("video-file").value = "";
    $("file-selected").hidden = true;
    $("drop-title").textContent = "把视频拖到这里";
    $("drop-copy").textContent = "或点击选择本地文件";
    $("file-hint").hidden = false;
  }

  function formError(message) {
    $("form-error").textContent = message;
    $("form-error").hidden = false;
  }

  function busy(value, label) {
    state.busy = value;
    $("start-button").disabled = value;
    $("start-button").classList.toggle("busy", value);
    $("start-button").querySelector(".button-label").textContent = label || (value ? "正在提交" : "开始译配");
    for (const id of ["video-file", "source-url", "source-language", "job-title", "upload-tab", "url-tab", "remove-file", "new-recovery-enabled", "new-recovery-source", "new-recovery-holds"]) $(id).disabled = value;
    $("dropzone").setAttribute("aria-disabled", String(value));
    $("submission-progress").hidden = !value;
    if (!value) $("upload-percent").textContent = "";
  }

  function upload(file, sourceLanguage, recoveryPolicy) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs/upload?name=" + encodeURIComponent(file.name) + "&source_language=" + encodeURIComponent(sourceLanguage)
        + "&website_recovery=" + encodeURIComponent(JSON.stringify(recoveryPolicy)));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.timeout = 30 * 60 * 1000;
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          const percent = Math.min(100, Math.round(event.loaded / event.total * 100));
          $("upload-progress").value = percent;
          $("upload-percent").textContent = percent + "%";
          $("submission-text").textContent = percent < 100 ? "正在上传视频" : "上传完成，等待接收确认";
        }
      };
      xhr.onload = () => {
        let data = null;
        try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (_) { /* Never insert response HTML. */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new RequestFailure(xhr.status, typeof data?.detail === "string" ? data.detail : data?.message || "Upload failed"));
      };
      xhr.onerror = () => reject(new RequestFailure(0, "network"));
      xhr.ontimeout = () => reject(new RequestFailure(0, "timeout"));
      xhr.onabort = () => reject(new RequestFailure(0, "上传已停止，请重新选择视频后再试。"));
      xhr.send(file);
    });
  }

  function parsedJob(data) {
    if (data?.job && typeof data.job === "object") return data.job;
    return data && typeof data === "object" ? data : null;
  }

  async function submitSource(source) {
    if (state.busy) return;
    $("form-error").hidden = true;
    $("upload-progress").value = 0;
    $("submission-text").textContent = source.kind === "upload" ? "正在上传视频" : "正在提交视频链接";
    busy(true, source.kind === "upload" ? "正在上传" : "正在提交");
    try {
      const recoveryPolicy = { enabled: $("new-recovery-enabled").checked,
        source_mode: $("new-recovery-source").value, picture_holds: $("new-recovery-holds").checked, reserve_gib: 8 };
      const response = source.kind === "upload" ? await upload(source.file, source.sourceLanguage || "auto", recoveryPolicy) : await request("/api/jobs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_url: source.url, title: source.title || "", source_language: source.sourceLanguage || "auto", website_recovery: recoveryPolicy }),
      });
      const job = parsedJob(response);
      const id = job?.id || response?.job_id;
      if (id) {
        state.sources.set(String(id), source);
        await selectJob(String(id), job?.status ? job : null);
        toast(job?.status === "failed" ? "任务暂未能开始，请查看任务提示。" : "素材已接收，可以在任务中查看实际进度。");
      } else {
        toast("素材已提交，正在更新任务列表。请确认列表后再重复提交。");
      }
      await refreshJobs();
    } catch (error) {
      formError(displayError(error, "提交没有完成，请稍后重试。"));
    } finally {
      busy(false);
    }
  }

  function percent(job) {
    if (job.status === "completed") return 100;
    if (job.progress === null || job.progress === undefined || job.progress === "") return null;
    const value = Number(job.progress);
    return Number.isFinite(value) ? Math.round(Math.max(0, Math.min(100, value))) : null;
  }

  function stageLabel(job) {
    if (job.status === "completed") return "审稿预览已生成";
    if (job.status === "failed" && job.failure?.title) return displayError(job.failure.title, "等待检查");
    if (job.status === "canceled" || job.status === "cancelled") return "任务已取消";
    const text = typeof job.stage === "string" ? job.stage.trim() : "";
    if (stageNames[text.toLowerCase()]) return stageNames[text.toLowerCase()];
    if (/[\u3400-\u9fff]/.test(text) && !/key|token|bearer|\/Users\/|\/home\//i.test(text)) return text.slice(0, 80);
    return job.status === "queued" ? "等待处理" : job.status === "failed" ? "处理未完成" : "正在处理视频";
  }

  function jobTitle(job) {
    return typeof job.title === "string" && job.title.trim() ? job.title : "未命名视频";
  }

  function dateLabel(raw) {
    if (!raw) return "";
    const numeric = typeof raw === "number" ? (raw < 1e12 ? raw * 1000 : raw) : raw;
    const date = new Date(numeric);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }

  function pill(status, failure = null) {
    const text = status === "failed" && failure?.title ? displayError(failure.title, "等待检查") : statusNames[status] || "状态待确认";
    const node = create("span", "status-pill", text);
    node.dataset.status = status || "unknown";
    return node;
  }

  function renderJobs() {
    const focused = document.activeElement?.dataset?.jobId;
    $("task-count").textContent = state.jobs.length ? String(state.jobs.length) : "";
    $("tasks-empty").hidden = state.jobs.length !== 0;
    $("jobs-list").replaceChildren();
    for (const original of state.jobs) {
      const job = String(original.id) === state.selectedId && state.selectedJob ? { ...original, ...state.selectedJob } : original;
      const id = String(job.id);
      const item = create("li");
      const button = create("button", "job-item");
      button.type = "button";
      button.dataset.jobId = id;
      button.dataset.status = job.status || "unknown";
      const selected = id === state.selectedId;
      button.classList.toggle("selected", selected);
      if (selected) button.setAttribute("aria-current", "true");
      const icon = create("span", "job-icon", job.status === "completed" ? "✓" : job.status === "failed" ? "!" : "▶");
      icon.setAttribute("aria-hidden", "true");
      const copy = create("span", "job-copy");
      copy.append(create("strong", "", jobTitle(job)));
      const meta = create("span", "job-meta");
      const date = dateLabel(job.created_at);
      if (date) meta.append(create("span", "", date));
      const p = percent(job);
      meta.append(create("span", "job-stage", stageLabel(job) + (job.status === "running" && p !== null ? " · " + p + "%" : "")));
      copy.append(meta);
      const right = create("span", "job-right");
      right.append(pill(job.status, job.failure), create("span", "job-chevron", "›"));
      button.append(icon, copy, right);
      button.addEventListener("click", () => selectJob(id, original));
      item.append(button);
      $("jobs-list").append(item);
      if (id === focused) button.focus({ preventScroll: true });
    }
  }

  function artifactUrl(value) {
    if (typeof value !== "string" || !value) return null;
    try {
      const url = new URL(value, window.location.href);
      return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }

  function artifactType(artifact, url) {
    const kind = String(artifact.kind || "").toLowerCase();
    const extension = new URL(url).pathname.split(".").pop().toLowerCase();
    if (kind.includes("video") || ["mp4", "mov", "webm", "m4v"].includes(extension)) return "video";
    if (kind.includes("audio") || ["wav", "mp3", "m4a", "aac", "flac", "ogg"].includes(extension)) return "audio";
    if (/subtitle|srt|vtt|ass/.test(kind) || ["srt", "ass", "vtt"].includes(extension)) return "subtitle";
    return "file";
  }

  function resetPreview() {
    for (const id of ["result-video", "result-audio"]) {
      const media = $(id);
      media.pause();
      media.removeAttribute("src");
      media.load();
      media.hidden = true;
    }
    state.previewKey = "";
    $("media-preview").hidden = true;
  }

  function renderArtifacts(job) {
    const artifacts = (Array.isArray(job.artifacts) ? job.artifacts : []).filter((a) => a && typeof a === "object")
      .map((a) => ({ ...a, safeUrl: artifactUrl(a.url) })).filter((a) => a.safeUrl);
    for (const item of artifacts) item.type = artifactType(item, item.safeUrl);
    const videos = artifacts.filter((a) => a.type === "video");
    const chosenUrl = $("episode-select").value;
    $("episode-preview").hidden = videos.length < 2;
    $("episode-select").replaceChildren();
    for (const video of videos) {
      const option = create("option", "", video.label || "分集视频");
      option.value = video.safeUrl;
      option.selected = chosenUrl === video.safeUrl;
      $("episode-select").append(option);
    }
    const preview = videos.find((a) => a.safeUrl === $("episode-select").value) || videos[0] || artifacts.find((a) => a.type === "audio");
    const key = preview ? String(job.id) + ":" + preview.type + ":" + preview.safeUrl : "";
    if (key !== state.previewKey) {
      resetPreview();
      if (preview) {
        const media = $(preview.type === "video" ? "result-video" : "result-audio");
        media.src = preview.safeUrl;
        media.hidden = false;
        $("media-preview").hidden = false;
        state.previewKey = key;
      }
    }
    $("downloads").hidden = artifacts.length === 0;
    $("download-links").replaceChildren();
    for (const artifact of artifacts) {
      const fallback = { video: "下载视频", audio: "下载音频", subtitle: "下载字幕", file: "下载文件" }[artifact.type];
      const link = create("a", "download-link");
      link.href = artifact.safeUrl;
      link.setAttribute("download", "");
      link.rel = "noopener";
      link.append(create("span", "", "↓"), create("span", "", typeof artifact.label === "string" ? artifact.label : fallback));
      $("download-links").append(link);
    }
  }

  function recoveryPolicy(value) {
    return { enabled: value?.enabled !== false, source_mode: value?.source_mode === "strict" ? "strict" : "contextual",
      picture_holds: value?.picture_holds !== false, reserve_gib: value?.reserve_gib === 2 ? 2 : 8 };
  }

  function recoveryDraft() {
    return { enabled: $("recovery-enabled").checked, source_mode: $("recovery-source").value,
      picture_holds: $("recovery-holds").checked, reserve_gib: Number($("recovery-reserve").value) };
  }

  function recoveryNeedsTextBudget(category) {
    return !["storage", "tts", "audio", "alignment", "timing", "picture"].includes(category);
  }

  function updateRecoveryAction() {
    const job = state.selectedJob;
    if (!job) return;
    const info = job.recovery || {}, policy = recoveryPolicy(info.policy), draft = recoveryDraft();
    const budget = info.budget;
    const budgetKnown = info.available !== false && budget && [budget.used, budget.max, budget.remaining].every(Number.isInteger)
      && budget.max === 6 && budget.used >= 0 && budget.used <= budget.max
      && budget.remaining === budget.max - budget.used && typeof budget.exhausted === "boolean"
      && budget.exhausted === (budget.remaining === 0);
    const exhausted = budgetKnown && budget.exhausted;
    const blocked = job.failure?.can_retry === false;
    const changed = info.policy_configured === false || JSON.stringify(policy) !== JSON.stringify(draft);
    const category = job.failure?.category;
    const textBudgetBlocked = exhausted && recoveryNeedsTextBudget(category);
    const sourceChanged = info.policy_configured === false || policy.enabled !== draft.enabled || policy.source_mode !== draft.source_mode;
    const permittedChange = changed && ((category === "source_review" && draft.enabled && draft.source_mode === "contextual" && sourceChanged)
      || ["tts", "audio", "alignment", "timing", "picture"].includes(category));
    $("recover-button").disabled = state.recoverPending || state.reviewSaving || !budgetKnown || textBudgetBlocked || (blocked && !permittedChange);
    $("retry-button").disabled = state.recoverPending || state.reviewSaving || blocked;
    $("recovery-action-note").textContent = !budgetKnown ? "恢复记录暂不可用，已有成果保留。请刷新后再继续。"
      : textBudgetBlocked ? "本任务的文字恢复预算已用完，已有成果已保留。"
      : blocked && !permittedChange ? "此问题仍需有效修订；重复点击或更改无关选项不会跳过内容检查。"
      : exhausted ? "文字恢复次数已用完；可继续本地处理，需要新的文字修订时仍会停止。"
      : "按当前选项从断点继续，只重做必要步骤。文字服务仅提供受限文字建议。";
    for (const id of ["recovery-enabled", "recovery-source", "recovery-holds", "recovery-reserve"]) $(id).disabled = state.recoverPending || state.reviewSaving;
  }

  function renderRecovery(job) {
    if (state.reviewJobId !== job.id) {
      state.reviewJobId = job.id;
      state.reviewRows = [];
      state.reviewLoaded = false;
      state.reviewLoading = false;
      $("review-panel").open = false;
      $("review-rows").replaceChildren();
      $("review-status").textContent = "";
      $("review-save").disabled = true;
    }
    const info = job.recovery || {}, policy = recoveryPolicy(info.policy), key = JSON.stringify(policy);
    if (state.recoveryDraftId !== job.id || (state.recoveryPolicyKey !== key && !state.recoveryDirty)) {
      $("recovery-enabled").checked = policy.enabled;
      $("recovery-source").value = policy.source_mode;
      $("recovery-holds").checked = policy.picture_holds;
      $("recovery-reserve").value = String(policy.reserve_gib);
      state.recoveryDraftId = job.id;
      state.recoveryPolicyKey = key;
      state.recoveryDirty = false;
    }
    $("recovery-low-reserve").disabled = job.failure?.category !== "storage" && policy.reserve_gib !== 2;
    const number = (value) => Number.isInteger(value) && value >= 0 && value <= 100000 ? value : 0;
    const calls = number(info.proxy_calls), used = number(info.budget?.used), maximum = number(info.budget?.max);
    const policyText = info.policy_configured === false ? "尚未设置本任务的网页恢复选项。"
      : `当前选项：${policy.enabled ? "辅助恢复已开启" : "辅助恢复已关闭"}；${policy.source_mode === "strict" ? "等待原词核对" : "疑词保守表达并保留标记"}；${policy.picture_holds ? "允许最长4秒停格" : "不自动停格"}；空间余量${policy.reserve_gib}GiB。`;
    $("recovery-summary").textContent = policyText + (calls ? ` Proxy调用记录${calls}次。` : " 尚无Proxy恢复调用记录。")
      + (maximum ? `文字恢复动作${used}/${maximum}次。` : "");
    $("recovery-events").replaceChildren();
    const actions = { source: "原词疑点复核", source_contextual: "原词疑点复核", compact: "局部台词精简", audio: "局部声音恢复", timing: "画面时间恢复", picture_hold: "画面停格处理" };
    const statuses = { completed: "已完成", succeeded: "已完成", success: "已完成", approved: "已保存建议", failed: "未完成", pending: "已记录请求", reserved: "已记录请求", started: "处理中", editing: "文字修订中", reviewing: "独立复核中", applying: "保存修订中", interrupted: "已中断，记录保留", stopped: "已停止", canceled: "已取消", cancelled: "已取消", reused: "已复用" };
    for (const event of (Array.isArray(info.events) ? info.events.slice(-8) : [])) {
      const label = actions[event?.action] || "恢复检查", status = statuses[event?.status] || "已记录";
      const count = number(event?.affected_count);
      $("recovery-events").append(create("li", "", `${label} · ${status}${count ? ` · ${count}句` : ""}${event?.proxy_called === true ? " · 已请求Proxy" : ""}`));
    }
    $("recovery-record").hidden = false;
    updateRecoveryAction();
  }

  async function loadReview() {
    const job = state.selectedJob;
    if (!job || !["failed", "canceled", "cancelled"].includes(job.status) || state.reviewLoading || state.reviewLoaded) return;
    const id = job.id;
    state.reviewLoading = true;
    $("review-status").textContent = "正在读取待核对的原句…";
    try {
      const data = await request("/api/jobs/" + encodeURIComponent(id) + "/review");
      if (state.selectedId !== id) return;
      if (!Array.isArray(data?.rows) || data.rows.length > 8) throw new Error("核对记录暂不可用。");
      state.reviewRows = data.rows;
      $("review-rows").replaceChildren();
      const stamp = (value) => Math.floor(value / 60) + "分" + String(Math.floor(value % 60)).padStart(2, "0") + "秒";
      data.rows.forEach((row, index) => {
        if (!/^line\d{4,}$/.test(row?.id) || typeof row.text_source !== "string" || typeof row.text_zh !== "string"
            || !Number.isFinite(row.start_s) || !Number.isFinite(row.end_s)) throw new Error("核对记录暂不可用。");
        const box = create("section", "review-row");
        box.append(create("div", "review-position", `第${index + 1}句 · 原片${stamp(row.start_s)}–${stamp(row.end_s)}${row.uncertain ? " · 原词仍有疑点" : ""}`));
        const audio = create("audio");
        audio.controls = true;
        audio.preload = "none";
        audio.src = "/api/jobs/" + encodeURIComponent(id) + "/review-audio/" + encodeURIComponent(row.id);
        audio.setAttribute("aria-label", `第${index + 1}句原声`);
        box.append(audio);
        for (const [field, label, value] of [["source", "原文", row.text_source], ["translation", "首选中文", row.text_zh]]) {
          const inputId = "review-" + field + "-" + row.id;
          const caption = create("label", "field-label", label);
          caption.htmlFor = inputId;
          const input = create("textarea");
          input.id = inputId;
          input.maxLength = 4000;
          input.value = value;
          input.addEventListener("input", () => { $("review-save").disabled = state.reviewSaving || state.recoverPending; });
          box.append(caption, input);
        }
        $("review-rows").append(box);
      });
      state.reviewLoaded = true;
      $("review-status").textContent = data.rows.length ? "只修改需要更正的句子，再保存。原声仅在本机处理。" : "当前没有需要人工核对的原句。";
      $("review-save").disabled = true;
    } catch (error) {
      if (state.selectedId === id) $("review-status").textContent = displayError(error, "暂时无法读取原句，请收起后重新展开。");
    } finally { if (state.selectedId === id) state.reviewLoading = false; }
  }

  async function saveReview() {
    if (!state.selectedId || state.reviewSaving || state.recoverPending) return;
    const id = state.selectedId, corrections = [];
    for (const row of state.reviewRows) {
      const source = $("review-source-" + row.id)?.value.trim(), translated = $("review-translation-" + row.id)?.value.trim();
      const correction = { id: row.id };
      if (source !== row.text_source.trim()) correction.source_text = source;
      if (translated !== row.text_zh.trim()) correction.preferred_translation = translated;
      if (Object.keys(correction).length > 1) corrections.push(correction);
    }
    if (!corrections.length) { $("review-status").textContent = "尚无更正，无需保存。"; return; }
    if (corrections.some((row) => Object.entries(row).some(([key, value]) => key !== "id" && !value))) {
      $("review-status").textContent = "原文和首选中文不能留空。";
      return;
    }
    state.reviewSaving = true;
    $("review-save").disabled = true;
    $("review-status").textContent = "正在保存更正…";
    updateRecoveryAction();
    try {
      const data = await request("/api/jobs/" + encodeURIComponent(id) + "/review", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ corrections }),
      });
      if (state.selectedId !== id) return;
      if (data?.saved !== true) throw new Error("更正尚未保存。");
      state.reviewRows = state.reviewRows.map((row) => ({ ...row,
        text_source: $("review-source-" + row.id).value.trim(), text_zh: $("review-translation-" + row.id).value.trim() }));
      $("review-status").textContent = "更正已另存。可点击“仅从断点重试”，受影响译稿会重新复核。";
      await refreshSelected(true);
    } catch (error) {
      if (state.selectedId === id) {
        $("review-status").textContent = displayError(error, "更正未保存，输入内容保留，请稍后再试。");
        $("review-save").disabled = false;
      }
    } finally { state.reviewSaving = false; updateRecoveryAction(); }
  }

  function renderSelected(job) {
    $("preview-empty").hidden = true;
    $("job-detail").hidden = false;
    $("detail-status").hidden = false;
    $("detail-status").textContent = job.status === "failed" && job.failure?.title
      ? displayError(job.failure.title, "等待检查") : statusNames[job.status] || "状态待确认";
    $("detail-status").dataset.status = job.status || "unknown";
    $("detail-title").textContent = jobTitle(job);
    const progress = percent(job);
    $("detail-stage").textContent = stageLabel(job);
    $("detail-percent").textContent = progress === null ? "—" : progress + "%";
    $("detail-progress").value = progress ?? 0;
    $("detail-date").textContent = dateLabel(job.created_at) ? "创建于 " + dateLabel(job.created_at) : "";
    const notes = {
      queued: "素材已接收，等待处理服务开始。",
      running: "当前阶段与进度由处理服务实时更新。",
      completed: "审稿预览已生成。请先试听音色、停顿与呼吸细节，再确认使用。",
      failed: "可以继续此任务，已完成且通过校验的步骤会复用。",
      canceled: "任务已停止，可随时继续处理。",
      cancelled: "任务已停止，可随时继续处理。",
    };
    const hasPilot = job.status === "running" && Array.isArray(job.artifacts)
      && job.artifacts.some((item) => item && item.kind === "audio");
    $("progress-note").textContent = hasPilot
      ? "完整意群的小样已可试听，当前为纯人声。其余配音继续生成，最终会切换为完整视频。"
      : notes[job.status] || "正在读取任务状态。";
    if (job.status === "failed" && job.failure) {
      const completed = Number(job.failure.completed), total = Number(job.failure.total);
      const count = Number.isInteger(completed) && Number.isInteger(total) && total > 0
        && completed >= 0 && completed <= total && total <= 100000
        ? `机器译稿 ${completed}/${total} 条已保存。` : "已通过检查的步骤均已保留。";
      $("progress-note").textContent = count + displayError(job.failure.next_action, "请先处理提示项目，再从断点继续。");
    }
    const retryable = ["failed", "canceled", "cancelled"].includes(job.status);
    $("job-error").hidden = !retryable;
    if (retryable) {
      const failure = job.status === "failed" ? job.failure : null;
      $("job-error-title").textContent = failure?.title ? displayError(failure.title, "等待检查")
        : job.status === "failed" ? "这次处理没有完成" : "这个任务已取消";
      let explanation = failure?.message ? displayError(failure.message)
        : job.status === "failed" ? displayError(job.error) : "需要时可以继续此任务。";
      if (failure?.category === "unknown" && job.error) {
        const actual = displayError(job.error);
        if (actual && !explanation.includes(actual)) explanation += " " + actual;
      }
      const checkLabels = new Set(["数字", "单位", "否定", "条件", "比例", "专名", "原文识别", "独立二审"]);
      const positions = Array.isArray(failure?.source_windows) ? failure.source_windows.slice(0, 8)
        .filter((item) => Number.isFinite(item?.start_s) && Number.isFinite(item?.end_s)
          && item.start_s >= 0 && item.end_s >= item.start_s && item.end_s <= 172800)
        .map((item) => {
          const stamp = (value) => Math.floor(value / 60) + "分" + String(Math.floor(value % 60)).padStart(2, "0") + "秒";
          const row = typeof item.id === "string" && /^line\d{4,}$/.test(item.id)
            ? "第" + Number(item.id.slice(4)) + "句" : "";
          const labels = Array.isArray(item.checks) ? item.checks.filter((label) => checkLabels.has(label)) : [];
          return row + (labels.length ? "（" + labels.join("、") + "）" : "")
            + " " + stamp(item.start_s) + "–" + stamp(item.end_s);
        }) : [];
      if (positions.length) explanation += " 原片位置：" + positions.join("、") + "。";
      $("job-error-copy").textContent = explanation;
      const blocked = failure?.can_retry === false;
      $("retry-button").textContent = blocked ? "等待有效修订" : "仅从断点重试";
      $("retry-button").disabled = blocked;
    }
    renderRecovery(job);
    $("cancel-button").hidden = !["queued", "running"].includes(job.status);
    $("cancel-button").disabled = state.cancelPending;
    renderArtifacts(job);
    announce(jobTitle(job) + "，" + stageLabel(job) + (progress === null ? "" : "，" + progress + "%"));
  }

  async function selectJob(id, preliminary = null) {
    if (state.selectedId !== id) {
      resetPreview();
      state.selectedJob = null;
    }
    state.selectedId = id;
    if (preliminary) {
      state.selectedJob = preliminary;
      renderSelected(preliminary);
    }
    const url = new URL(window.location.href);
    url.searchParams.set("job", id);
    window.history.replaceState(null, "", url);
    renderJobs();
    await refreshSelected(true);
  }

  async function refreshSelected(force = false) {
    if (!state.selectedId || (state.detailPending && !force)) return;
    const id = state.selectedId;
    const revision = ++state.detailRevision;
    state.detailPending = true;
    try {
      const data = parsedJob(await request("/api/jobs/" + encodeURIComponent(id)));
      if (state.selectedId !== id || revision !== state.detailRevision) return;
      if (!data || !data.id) throw new RequestFailure(0, "暂时无法读取任务详情，请稍后刷新。");
      state.selectedJob = data;
      renderSelected(data);
      renderJobs();
    } catch (error) {
      if (state.selectedId !== id || revision !== state.detailRevision) return;
      if (!state.selectedJob) {
        $("job-detail").hidden = true;
        $("detail-status").hidden = true;
        $("preview-empty").hidden = false;
        $("empty-title").textContent = "暂时无法读取任务";
        $("empty-copy").textContent = displayError(error, "请稍后刷新，或在下方选择其他任务。");
      } else {
        $("progress-note").textContent = "连接暂时中断，以上为最近收到的进度。正在尝试更新。";
      }
    } finally {
      if (revision === state.detailRevision) state.detailPending = false;
    }
  }

  async function refreshJobs() {
    if (state.listPending) return;
    state.listPending = true;
    try {
      const data = await request("/api/jobs");
      if (!Array.isArray(data?.jobs)) throw new RequestFailure(0, "暂时无法读取任务列表。");
      state.jobs = data.jobs.filter((job) => job && typeof job === "object" && job.id);
      $("list-error").hidden = true;
      renderJobs();
      if (!state.selectedId && state.jobs.length) await selectJob(String(state.jobs[0].id), state.jobs[0]);
    } catch (error) {
      $("list-error").textContent = displayError(error, "暂时无法读取任务列表，请稍后刷新。");
      $("list-error").hidden = false;
      if (!state.jobs.length) $("tasks-empty").hidden = true;
    } finally {
      state.listPending = false;
    }
  }

  function healthView(data) {
    // A health response proves connectivity, not that a whole dubbing pipeline
    // or listening review has passed. Job state remains the processing truth.
    const notice = data?.ready === false ? "服务已连接，处理准备尚未完成。可以先选择素材，稍后再提交；实际进度以任务状态为准。" : "";
    return ["online", "服务已连接", notice];
  }

  async function refreshHealth() {
    if (state.healthPending) return;
    state.healthPending = true;
    try {
      const data = await request("/api/health");
      const [kind, label, notice] = healthView(data);
      $("connection").dataset.state = kind;
      $("connection-text").textContent = label;
      $("service-notice").hidden = !notice;
      $("service-notice-text").textContent = notice;
    } catch (_) {
      $("connection").dataset.state = "offline";
      $("connection-text").textContent = "服务暂未连接";
      $("service-notice").hidden = false;
      $("service-notice-text").textContent = "暂时无法连接处理服务。可以先选择素材，连接恢复后再提交。";
    } finally {
      state.healthPending = false;
    }
  }

  $("source-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.busy) return;
    if (state.mode === "upload") {
      if (!state.file) { formError("请先选择一段本地视频。"); $("dropzone").focus(); return; }
      submitSource({ kind: "upload", file: state.file, sourceLanguage: $("source-language").value });
    } else {
      let url;
      try {
        url = new URL($("source-url").value.trim());
        if (!["https:", "http:"].includes(url.protocol)) throw new Error("Invalid video URL");
      } catch (_) {
        formError("请填写有效的视频链接，以 https:// 或 http:// 开头。");
        $("source-url").focus();
        return;
      }
      submitSource({ kind: "url", url: url.href, title: $("job-title").value.trim(), sourceLanguage: $("source-language").value });
    }
  });
  $("episode-select").addEventListener("change", () => {
    if (state.selectedJob) renderArtifacts(state.selectedJob);
  });
  for (const name of ["upload", "url"]) {
    $(name + "-tab").addEventListener("click", () => setMode(name));
    $(name + "-tab").addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const target = event.key === "Home" ? "upload" : event.key === "End" ? "url" : state.mode === "upload" ? "url" : "upload";
        setMode(target, true);
      }
    });
  }
  $("dropzone").addEventListener("click", () => { if (!state.busy) $("video-file").click(); });
  $("dropzone").addEventListener("keydown", (event) => {
    if (["Enter", " "].includes(event.key)) { event.preventDefault(); if (!state.busy) $("video-file").click(); }
  });
  $("video-file").addEventListener("change", (event) => chooseFile(event.target.files[0]));
  $("remove-file").addEventListener("click", removeFile);
  for (const name of ["dragenter", "dragover"]) $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    if (!state.busy) $("dropzone").classList.add("dragover");
  });
  for (const name of ["dragleave", "drop"]) $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.remove("dragover");
  });
  $("dropzone").addEventListener("drop", (event) => {
    if (!state.busy) chooseFile(event.dataTransfer.files[0]);
  });
  $("refresh-jobs").addEventListener("click", () => Promise.allSettled([refreshJobs(), refreshSelected()]));
  $("reconnect-button").addEventListener("click", () => Promise.allSettled([refreshHealth(), refreshJobs(), refreshSelected()]));
  $("cancel-button").addEventListener("click", async () => {
    if (!state.selectedId || state.cancelPending) return;
    const id = state.selectedId;
    state.cancelPending = true;
    $("cancel-button").disabled = true;
    try {
      await request("/api/jobs/" + encodeURIComponent(id) + "/cancel", { method: "POST" });
      toast("取消请求已提交，正在更新任务状态。");
      await Promise.allSettled([refreshSelected(true), refreshJobs()]);
    } catch (error) { toast(displayError(error, "暂时无法取消任务，请稍后重试。")); }
    finally { state.cancelPending = false; $("cancel-button").disabled = false; }
  });
  $("retry-button").addEventListener("click", async () => {
    if (!state.selectedJob || state.busy || state.recoverPending) return;
    if (state.selectedJob.failure?.can_retry === false) {
      toast("当前没有新的修订，请先核对提示的内容。");
      return;
    }
    state.recoverPending = true;
    updateRecoveryAction();
    try {
      const result = await request("/api/jobs/" + encodeURIComponent(state.selectedId) + "/retry", { method: "POST" });
      await selectJob(String(result.id), parsedJob(result));
      await refreshJobs();
      toast("任务已继续，会复用通过校验的已有步骤。");
    } catch (error) { toast(displayError(error, "暂时无法继续，请稍后重试。")); }
    finally { state.recoverPending = false; updateRecoveryAction(); }
  });
  for (const id of ["recovery-enabled", "recovery-source", "recovery-holds", "recovery-reserve"]) $(id).addEventListener("change", () => {
    state.recoveryDirty = true;
    updateRecoveryAction();
  });
  $("review-panel").addEventListener("toggle", () => { if ($("review-panel").open) loadReview(); });
  $("review-save").addEventListener("click", saveReview);
  $("recover-button").addEventListener("click", async () => {
    if (!state.selectedId || state.recoverPending || $("recover-button").disabled) return;
    const id = state.selectedId, policy = recoveryDraft();
    state.recoverPending = true;
    updateRecoveryAction();
    try {
      const result = await request("/api/jobs/" + encodeURIComponent(id) + "/recover", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ policy }),
      });
      state.recoveryDirty = false;
      await selectJob(String(result.id), parsedJob(result));
      await refreshJobs();
      toast("恢复选项已保存，任务已排队；实际恢复动作会记录在这里。");
    } catch (error) { toast(displayError(error, "恢复没有开始，已有成果保留。请刷新查看诊断。")); }
    finally { state.recoverPending = false; updateRecoveryAction(); }
  });
  for (const id of ["result-video", "result-audio"]) $(id).addEventListener("error", () => {
    if ($(id).getAttribute("src")) toast("这个文件暂时无法预览，可以尝试下载或稍后刷新。");
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) Promise.allSettled([refreshHealth(), refreshJobs(), refreshSelected()]);
  });

  const initialId = new URL(window.location.href).searchParams.get("job");
  if (initialId) state.selectedId = initialId;
  Promise.allSettled([refreshHealth(), refreshJobs(), refreshSelected()]);
  window.setInterval(() => { if (!document.hidden) refreshSelected(); }, 1500);
  window.setInterval(() => { if (!document.hidden) refreshJobs(); }, 4000);
  window.setInterval(() => { if (!document.hidden) refreshHealth(); }, 8000);
})();
