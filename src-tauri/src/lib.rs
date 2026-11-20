use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter};
use tauri_plugin_shell::ShellExt;
use tokio::sync::Semaphore;
use walkdir::WalkDir;

const SUPPORTED_EXTENSIONS: [&str; 6] = ["hwp", "hwpx", "pdf", "docx", "xlsx", "pptx"];
const MAX_CONCURRENCY: usize = 4;
const PER_FILE_TIMEOUT: Duration = Duration::from_secs(600);
const PROGRESS_EVENT: &str = "conversion-progress";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TargetFile {
    pub id: usize,
    pub path: String,
    pub file_name: String,
    pub extension: String,
    pub size: u64,
    /// Output path relative to the chosen output folder (keeps folder hierarchy).
    pub relative_out_path: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ProgressPayload {
    pub id: usize,
    /// "converting" | "done" | "error"
    pub status: String,
    pub output_path: Option<String>,
    pub error: Option<String>,
    pub completed: usize,
    pub total: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct BatchSummary {
    pub total: usize,
    pub succeeded: usize,
    pub failed: usize,
    pub output_dir: String,
}

#[derive(Debug, Deserialize)]
struct EngineResult {
    success: bool,
    #[allow(dead_code)]
    file_name: Option<String>,
    markdown: Option<String>,
    error: Option<String>,
}

fn supported_ext(path: &Path) -> Option<String> {
    let ext = path.extension()?.to_str()?.to_ascii_lowercase();
    SUPPORTED_EXTENSIONS.contains(&ext.as_str()).then_some(ext)
}

fn is_ignored(path: &Path) -> bool {
    path.file_name()
        .and_then(|n| n.to_str())
        .map(|n| n.starts_with("~$") || n.starts_with("._"))
        .unwrap_or(false)
}

/// Builds `<rel_dir>/<stem>.md`, disambiguating collisions such as a.pdf + a.docx.
fn make_out_path(rel_source: &Path, ext: &str, used: &mut HashSet<String>) -> String {
    let stem = rel_source
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "document".into());
    let parent = rel_source.parent().map(Path::to_path_buf).unwrap_or_default();

    let mut candidate = parent.join(format!("{stem}.md"));
    if used.contains(&key(&candidate)) {
        candidate = parent.join(format!("{stem} ({ext}).md"));
        let mut n = 2;
        while used.contains(&key(&candidate)) {
            candidate = parent.join(format!("{stem} ({ext}) {n}.md"));
            n += 1;
        }
    }
    used.insert(key(&candidate));
    candidate.to_string_lossy().to_string()
}

fn key(p: &Path) -> String {
    p.to_string_lossy().to_lowercase()
}

#[tauri::command]
fn collect_target_files(paths: Vec<String>) -> Result<Vec<TargetFile>, String> {
    let mut files = Vec::new();
    let mut seen_sources = HashSet::new();
    let mut used_outputs = HashSet::new();

    let mut push = |source: &Path, rel_source: &Path, ext: String, files: &mut Vec<TargetFile>| {
        let canonical = source.canonicalize().unwrap_or_else(|_| source.to_path_buf());
        if !seen_sources.insert(canonical) {
            return;
        }
        let size = std::fs::metadata(source).map(|m| m.len()).unwrap_or(0);
        files.push(TargetFile {
            id: files.len(),
            path: source.to_string_lossy().to_string(),
            file_name: source
                .file_name()
                .map(|n| n.to_string_lossy().to_string())
                .unwrap_or_default(),
            extension: ext.clone(),
            size,
            relative_out_path: make_out_path(rel_source, &ext, &mut used_outputs),
        });
    };

    for raw in paths {
        let root = PathBuf::from(&raw);
        if root.is_file() {
            if is_ignored(&root) {
                continue;
            }
            if let Some(ext) = supported_ext(&root) {
                let rel = PathBuf::from(root.file_name().unwrap_or_default());
                push(&root, &rel, ext, &mut files);
            }
        } else if root.is_dir() {
            // Keep the dropped folder's own name as the top level of the output tree.
            let base = root.parent().map(Path::to_path_buf).unwrap_or_else(|| root.clone());
            let walker = WalkDir::new(&root)
                .follow_links(false)
                .sort_by_file_name()
                .into_iter()
                .filter_map(Result::ok)
                .filter(|e| e.file_type().is_file());
            for entry in walker {
                let p = entry.path();
                if is_ignored(p) {
                    continue;
                }
                if let Some(ext) = supported_ext(p) {
                    let rel = p
                        .strip_prefix(&base)
                        .map(Path::to_path_buf)
                        .unwrap_or_else(|_| PathBuf::from(p.file_name().unwrap_or_default()));
                    push(p, &rel, ext, &mut files);
                }
            }
        }
    }
    Ok(files)
}

fn parse_engine_output(stdout: &[u8]) -> Result<EngineResult, String> {
    if let Ok(r) = serde_json::from_slice::<EngineResult>(stdout) {
        return Ok(r);
    }
    // Tolerate stray output before the JSON object.
    let text = String::from_utf8_lossy(stdout);
    if let Some(start) = text.find("{\"success\"") {
        if let Ok(r) = serde_json::from_str::<EngineResult>(&text[start..]) {
            return Ok(r);
        }
    }
    Err("변환 엔진 응답을 해석할 수 없습니다.".into())
}

async fn convert_one(app: &AppHandle, file: &TargetFile, output_dir: &Path) -> Result<String, String> {
    let out_path = output_dir.join(&file.relative_out_path);
    if let Some(parent) = out_path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("출력 폴더 생성 실패: {e}"))?;
    }
    let out_arg = out_path.to_string_lossy().to_string();

    // --out lets the engine save extracted images in "<stem>_images/" next to the .md.
    let command = app
        .shell()
        .sidecar("converter-engine")
        .map_err(|e| format!("변환 엔진을 찾을 수 없습니다: {e}"))?
        .args([file.path.as_str(), "--out", out_arg.as_str()]);

    let output = tokio::time::timeout(PER_FILE_TIMEOUT, command.output())
        .await
        .map_err(|_| "변환 시간이 초과되었습니다 (10분).".to_string())?
        .map_err(|e| format!("변환 엔진 실행 실패: {e}"))?;

    let result = parse_engine_output(&output.stdout).map_err(|e| {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.trim().is_empty() {
            e
        } else {
            format!("{e}\n{}", stderr.trim())
        }
    })?;

    if !result.success {
        return Err(result.error.unwrap_or_else(|| "알 수 없는 오류".into()));
    }

    tokio::fs::write(&out_path, result.markdown.unwrap_or_default())
        .await
        .map_err(|e| format!("파일 저장 실패: {e}"))?;
    Ok(out_path.to_string_lossy().to_string())
}

#[tauri::command]
async fn convert_batch(
    app: AppHandle,
    files: Vec<TargetFile>,
    output_dir: String,
) -> Result<BatchSummary, String> {
    let out_root = PathBuf::from(&output_dir);
    tokio::fs::create_dir_all(&out_root)
        .await
        .map_err(|e| format!("출력 폴더를 만들 수 없습니다: {e}"))?;

    let total = files.len();
    let semaphore = Arc::new(Semaphore::new(MAX_CONCURRENCY));
    let completed = Arc::new(AtomicUsize::new(0));
    let succeeded = Arc::new(AtomicUsize::new(0));
    let out_root = Arc::new(out_root);

    let mut handles = Vec::with_capacity(total);
    for file in files {
        let app = app.clone();
        let semaphore = semaphore.clone();
        let completed = completed.clone();
        let succeeded = succeeded.clone();
        let out_root = out_root.clone();

        handles.push(tokio::spawn(async move {
            let _permit = semaphore.acquire_owned().await.expect("semaphore closed");
            let _ = app.emit(
                PROGRESS_EVENT,
                ProgressPayload {
                    id: file.id,
                    status: "converting".into(),
                    output_path: None,
                    error: None,
                    completed: completed.load(Ordering::SeqCst),
                    total,
                },
            );

            let result = convert_one(&app, &file, &out_root).await;
            let done = completed.fetch_add(1, Ordering::SeqCst) + 1;
            let payload = match result {
                Ok(path) => {
                    succeeded.fetch_add(1, Ordering::SeqCst);
                    ProgressPayload {
                        id: file.id,
                        status: "done".into(),
                        output_path: Some(path),
                        error: None,
                        completed: done,
                        total,
                    }
                }
                Err(err) => ProgressPayload {
                    id: file.id,
                    status: "error".into(),
                    output_path: None,
                    error: Some(err),
                    completed: done,
                    total,
                },
            };
            let _ = app.emit(PROGRESS_EVENT, payload);
        }));
    }

    for h in handles {
        let _ = h.await;
    }

    let ok = succeeded.load(Ordering::SeqCst);
    Ok(BatchSummary {
        total,
        succeeded: ok,
        failed: total - ok,
        output_dir,
    })
}

#[tauri::command]
async fn read_markdown(path: String) -> Result<String, String> {
    tokio::fs::read_to_string(&path)
        .await
        .map_err(|e| format!("파일을 읽을 수 없습니다: {e}"))
}

#[tauri::command]
fn open_output_folder(path: String) -> Result<(), String> {
    let target = PathBuf::from(&path);
    if !target.exists() {
        return Err(format!("폴더가 존재하지 않습니다: {path}"));
    }

    #[cfg(target_os = "windows")]
    let program = "explorer";
    #[cfg(target_os = "macos")]
    let program = "open";
    #[cfg(all(unix, not(target_os = "macos")))]
    let program = "xdg-open";

    std::process::Command::new(program)
        .arg(&target)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("폴더를 열 수 없습니다: {e}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            collect_target_files,
            convert_batch,
            read_markdown,
            open_output_folder
        ])
        .run(tauri::generate_context!())
        .expect("error while running doc2md");
}
