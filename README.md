# doc2md

HWP · HWPX · PDF · DOCX · XLSX · PPTX 문서를 마크다운(.md)으로 일괄 변환하는 Tauri v2 데스크톱 앱.

## 구조
- `src/` — Vanilla HTML/CSS/JS 프론트엔드 (`withGlobalTauri`)
- `src-tauri/` — Rust 백엔드 (`collect_target_files`, `convert_batch`, `read_markdown`, `open_output_folder`)
- `python-engine/` — MarkItDown + 커스텀 HWP/HWPX 변환기, PyInstaller 사이드카

## 빌드
```bash
py -3 -m venv python-engine/venv
python-engine/venv/Scripts/python -m pip install "markitdown[all]" olefile pyinstaller
npm install
npm run build:engine     # -> src-tauri/binaries/converter-engine-<triple>.exe
npm run tauri build      # -> src-tauri/target/release/bundle/{nsis,msi}
```
