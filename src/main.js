"use strict";

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const dialog = window.__TAURI__.dialog;

const EXTENSIONS = ["hwp", "hwpx", "pdf", "docx", "xlsx", "pptx"];
const STATUS_LABEL = { pending: "대기", converting: "변환중", done: "완료", error: "실패" };

const state = {
  files: [],          // TargetFile + { status, output_path, error }
  outputDir: null,
  running: false,
  finished: false,
  selectedId: null,
  startedAt: 0,
};

const $ = (id) => document.getElementById(id);
const el = {
  dropzone: $("dropzone"),
  overlay: $("drag-overlay"),
  list: $("file-list"),
  listHead: $("list-head"),
  fileCount: $("file-count"),
  btnStart: $("btn-start"),
  btnOpen: $("btn-open-folder"),
  btnReset: $("btn-reset"),
  btnFiles: $("btn-pick-files"),
  btnFolder: $("btn-pick-folder"),
  strip: $("progress-strip"),
  dot: $("progress-dot"),
  label: $("progress-label"),
  count: $("progress-count"),
  statOk: $("stat-ok"),
  statFail: $("stat-fail"),
  outChip: $("out-dir-chip"),
  barFill: $("bar-fill"),
  previewHead: $("preview-head"),
  previewType: $("preview-type"),
  previewName: $("preview-name"),
  previewPath: $("preview-path"),
  previewStats: $("preview-stats"),
  previewEmpty: $("preview-empty"),
  mdView: $("md-view"),
  mdCode: $("md-code"),
  errorView: $("error-view"),
  errorText: $("error-text"),
  btnCopy: $("btn-copy"),
  toast: $("toast"),
};

let currentMarkdown = "";

/* ------------------------------------------------------------------ utils */

function toast(msg, ms = 2600) {
  el.toast.textContent = msg;
  el.toast.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.toast.classList.remove("show"), ms);
}

function formatSize(bytes) {
  if (!bytes) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), u.length - 1);
  return `${(bytes / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

function typeClass(ext) {
  return ext === "hwpx" ? "hwp" : ext;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function counts() {
  let done = 0, error = 0;
  for (const f of state.files) {
    if (f.status === "done") done++;
    else if (f.status === "error") error++;
  }
  return { done, error, finished: done + error, total: state.files.length };
}

/* ------------------------------------------------------------------ rendering */

function fileItemHtml(f) {
  const cls = typeClass(f.extension);
  const badge =
    f.status === "converting"
      ? `<span class="badge converting"><span class="spinner"></span>${STATUS_LABEL.converting}</span>`
      : `<span class="badge ${f.status}">${STATUS_LABEL[f.status]}</span>`;
  const sub =
    f.status === "error"
      ? `<span class="err">${escapeHtml(firstLine(f.error || ""))}</span>`
      : `${escapeHtml(f.relative_out_path)} · ${formatSize(f.size)}`;
  return `
    <div class="ftype ${cls}">${f.extension.toUpperCase()}</div>
    <div class="file-info">
      <div class="file-name" title="${escapeHtml(f.path)}">${escapeHtml(f.file_name)}</div>
      <div class="file-sub">${sub}</div>
    </div>
    ${badge}`;
}

function firstLine(s) {
  return s.split("\n")[0];
}

function renderList() {
  el.list.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const f of state.files) {
    const li = document.createElement("li");
    li.className = "file";
    li.dataset.id = f.id;
    li.style.animationDelay = `${Math.min(f.id, 20) * 15}ms`;
    li.innerHTML = fileItemHtml(f);
    syncItemClasses(li, f);
    frag.appendChild(li);
  }
  el.list.appendChild(frag);

  const has = state.files.length > 0;
  el.listHead.hidden = !has;
  el.fileCount.textContent = state.files.length;
  el.dropzone.classList.toggle("compact", has);
  el.btnReset.disabled = !has || state.running;
  el.btnStart.hidden = !(has && !state.running && !state.finished);
}

function syncItemClasses(li, f) {
  li.classList.toggle("clickable", f.status === "done" || f.status === "error");
  li.classList.toggle("selected", f.id === state.selectedId);
}

function updateItem(f) {
  const li = el.list.querySelector(`li[data-id="${f.id}"]`);
  if (!li) return;
  li.innerHTML = fileItemHtml(f);
  syncItemClasses(li, f);
}

function renderProgress() {
  const { done, error, finished, total } = counts();
  el.strip.hidden = !state.outputDir;
  const pct = total ? Math.round((finished / total) * 100) : 0;
  el.barFill.style.width = `${pct}%`;
  el.barFill.classList.toggle("complete", state.finished);
  el.count.textContent = `${finished} / ${total} · ${pct}%`;
  el.statOk.innerHTML = `<i class="c-ok"></i>완료 ${done}`;
  el.statFail.innerHTML = `<i class="c-fail"></i>실패 ${error}`;
  el.statFail.hidden = error === 0;
  if (state.outputDir) {
    el.outChip.textContent = state.outputDir;
    el.outChip.title = state.outputDir;
  }

  el.dot.className = "dot";
  if (state.running) {
    el.dot.classList.add("running");
    el.label.textContent = "변환 중…";
  } else if (state.finished) {
    el.dot.classList.add(error ? "warn" : "done");
    const secs = ((Date.now() - state.startedAt) / 1000).toFixed(1);
    el.label.textContent = error ? `완료 (실패 ${error}건) · ${secs}s` : `모두 완료 · ${secs}s`;
  }
  el.btnOpen.disabled = !(state.finished && done > 0);
}

/* ------------------------------------------------------------------ preview */

function highlightMarkdown(md) {
  const lines = md.split("\n");
  let inComment = false;
  return lines
    .map((raw) => {
      const line = escapeHtml(raw);
      let html = line;
      if (inComment || raw.trimStart().startsWith("<!--")) {
        inComment = !raw.includes("-->");
        html = `<span class="cm">${line}</span>`;
      } else if (/^#{1,6}\s/.test(raw)) {
        html = `<span class="h">${line}</span>`;
      } else if (/^\s*\|/.test(raw)) {
        html = `<span class="tbl">${line.replace(/(?<!\\)\|/g, "<b>|</b>")}</span>`;
      } else if (/^\s*([-*+]|\d+\.)\s/.test(raw)) {
        html = `<span class="li">${line.replace(/^(\s*)([-*+]|\d+\.)/, "$1<b>$2</b>")}</span>`;
      }
      return `<span class="ln">${html || " "}</span>`;
    })
    .join("");
}

function showEmptyPreview() {
  el.previewHead.hidden = true;
  el.mdView.hidden = true;
  el.errorView.hidden = true;
  el.previewEmpty.hidden = false;
  currentMarkdown = "";
}

async function selectFile(id) {
  const f = state.files.find((x) => x.id === id);
  if (!f || (f.status !== "done" && f.status !== "error")) return;
  state.selectedId = id;
  el.list.querySelectorAll("li.file").forEach((li) => {
    li.classList.toggle("selected", Number(li.dataset.id) === id);
  });

  el.previewEmpty.hidden = true;
  el.previewHead.hidden = false;
  el.previewName.textContent = f.relative_out_path.split(/[\\/]/).pop();
  el.previewPath.textContent = f.output_path || f.path;
  el.previewPath.title = f.output_path || f.path;

  if (f.status === "error") {
    el.previewType.className = "ftype error-type pdf";
    el.previewType.textContent = "ERR";
    el.mdView.hidden = true;
    el.errorView.hidden = false;
    el.errorText.textContent = f.error || "알 수 없는 오류";
    el.previewStats.textContent = "";
    el.btnCopy.disabled = true;
    currentMarkdown = "";
    return;
  }

  el.previewType.className = "ftype md";
  el.previewType.textContent = "MD";
  el.errorView.hidden = true;
  el.btnCopy.disabled = false;
  try {
    const md = await invoke("read_markdown", { path: f.output_path });
    if (state.selectedId !== id) return;
    currentMarkdown = md;
    el.mdCode.innerHTML = md.trim() ? highlightMarkdown(md) : `<span class="ln cm">(추출된 텍스트가 없습니다)</span>`;
    el.mdView.hidden = false;
    el.mdView.scrollTop = 0;
    const lines = md ? md.split("\n").length : 0;
    el.previewStats.textContent = `${lines.toLocaleString()}줄 · ${md.length.toLocaleString()}자`;
  } catch (e) {
    el.mdView.hidden = true;
    el.errorView.hidden = false;
    el.errorText.textContent = String(e);
  }
}

async function copyMarkdown() {
  if (!currentMarkdown) return;
  try {
    await navigator.clipboard.writeText(currentMarkdown);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = currentMarkdown;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  const label = el.btnCopy.querySelector("span");
  el.btnCopy.classList.add("copied");
  label.textContent = "복사됨";
  setTimeout(() => {
    el.btnCopy.classList.remove("copied");
    label.textContent = "복사";
  }, 1500);
}

/* ------------------------------------------------------------------ flow */

async function handlePaths(paths) {
  if (!paths || paths.length === 0) return;
  if (state.running) {
    toast("변환이 진행 중입니다. 완료 후 다시 시도해 주세요.");
    return;
  }
  let files;
  try {
    files = await invoke("collect_target_files", { paths });
  } catch (e) {
    toast(`파일 목록을 읽지 못했습니다: ${e}`);
    return;
  }
  if (!files.length) {
    toast("지원되는 문서(HWP, HWPX, PDF, DOCX, XLSX, PPTX)가 없습니다.");
    return;
  }

  state.files = files.map((f) => ({ ...f, status: "pending", output_path: null, error: null }));
  state.outputDir = null;
  state.finished = false;
  state.selectedId = null;
  showEmptyPreview();
  renderList();
  renderProgress();
  toast(`${files.length}개 문서를 찾았습니다. 저장할 폴더를 선택하세요.`);
  await chooseOutputAndStart();
}

async function chooseOutputAndStart() {
  const outputDir = await dialog.open({
    directory: true,
    multiple: false,
    title: "변환된 마크다운을 저장할 폴더 선택",
  });
  if (!outputDir) {
    toast("저장 폴더를 선택하지 않았습니다. '변환 시작'을 눌러 다시 선택할 수 있습니다.");
    renderList();
    return;
  }
  await startConversion(outputDir);
}

async function startConversion(outputDir) {
  state.outputDir = outputDir;
  state.running = true;
  state.finished = false;
  state.startedAt = Date.now();
  for (const f of state.files) {
    f.status = "pending";
    f.error = null;
    f.output_path = null;
  }
  renderList();
  renderProgress();

  const payload = state.files.map(({ id, path, file_name, extension, size, relative_out_path }) => ({
    id, path, file_name, extension, size, relative_out_path,
  }));

  try {
    const summary = await invoke("convert_batch", { files: payload, outputDir });
    state.running = false;
    state.finished = true;
    renderList();
    renderProgress();
    el.btnOpen.classList.remove("pulse");
    void el.btnOpen.offsetWidth;
    el.btnOpen.classList.add("pulse");
    toast(
      summary.failed
        ? `변환 완료: 성공 ${summary.succeeded}건, 실패 ${summary.failed}건`
        : `${summary.succeeded}개 문서를 모두 변환했습니다 🎉`
    );
  } catch (e) {
    state.running = false;
    state.finished = true;
    renderList();
    renderProgress();
    toast(`변환을 시작하지 못했습니다: ${e}`, 4000);
  }
}

function onProgress({ payload }) {
  const f = state.files.find((x) => x.id === payload.id);
  if (!f || !state.running) return;
  f.status = payload.status;
  if (payload.output_path) f.output_path = payload.output_path;
  if (payload.error) f.error = payload.error;
  updateItem(f);
  renderProgress();
  // Auto-preview the first successful result.
  if (f.status === "done" && state.selectedId === null) selectFile(f.id);
}

async function pickFiles() {
  const selected = await dialog.open({
    multiple: true,
    directory: false,
    title: "변환할 문서 선택",
    filters: [{ name: "지원 문서", extensions: EXTENSIONS }],
  });
  if (selected) handlePaths(Array.isArray(selected) ? selected : [selected]);
}

async function pickFolder() {
  const selected = await dialog.open({ multiple: true, directory: true, title: "변환할 폴더 선택" });
  if (selected) handlePaths(Array.isArray(selected) ? selected : [selected]);
}

function reset() {
  if (state.running) return;
  state.files = [];
  state.outputDir = null;
  state.finished = false;
  state.selectedId = null;
  showEmptyPreview();
  renderList();
  renderProgress();
  el.btnOpen.disabled = true;
}

/* ------------------------------------------------------------------ wiring */

function setDragging(on) {
  el.overlay.classList.toggle("show", on);
  el.dropzone.classList.toggle("over", on);
}

async function init() {
  await listen("conversion-progress", onProgress);
  await listen("tauri://drag-enter", () => setDragging(true));
  await listen("tauri://drag-over", () => setDragging(true));
  await listen("tauri://drag-leave", () => setDragging(false));
  await listen("tauri://drag-drop", (e) => {
    setDragging(false);
    handlePaths(e.payload?.paths || []);
  });

  el.dropzone.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    pickFiles();
  });
  el.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      pickFiles();
    }
  });
  el.btnFiles.addEventListener("click", (e) => { e.stopPropagation(); pickFiles(); });
  el.btnFolder.addEventListener("click", (e) => { e.stopPropagation(); pickFolder(); });
  el.btnStart.addEventListener("click", chooseOutputAndStart);
  el.btnReset.addEventListener("click", reset);
  el.btnCopy.addEventListener("click", copyMarkdown);
  el.btnOpen.addEventListener("click", async () => {
    if (!state.outputDir) return;
    try {
      await invoke("open_output_folder", { path: state.outputDir });
    } catch (e) {
      toast(String(e));
    }
  });
  el.list.addEventListener("click", (e) => {
    const li = e.target.closest("li.file");
    if (li) selectFile(Number(li.dataset.id));
  });

  // Prevent the webview from navigating to dropped files.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c" && currentMarkdown && !window.getSelection().toString()) {
      copyMarkdown();
    }
  });

  renderList();
  renderProgress();
}

init();
