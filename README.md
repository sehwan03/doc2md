# doc2md

HWP · HWPX · PDF · DOCX · XLSX · PPTX 문서를 마크다운(.md)으로 일괄 변환하는 Tauri v2 데스크톱 앱.

## 이미지 처리 (PPTX · DOCX)
AI가 읽기 좋은 마크다운을 목표로, 이미지는 base64로 넣지 않고 `<문서이름>_images/` 폴더에 따로 저장한 뒤 상대 경로로 링크합니다.

```markdown
![슬라이드8 그림1 · 443×334 · ~197토큰](강의자료_images/s08_01.png)
```

- 링크에 슬라이드 번호 · 크기 · 예상 이미지 토큰(≈ 가로×세로÷750) · 대체 텍스트를 남겨, 이미지를 열지 않고도 필요 여부를 판단할 수 있습니다.
- 긴 변 1568px로 축소, 투명 · 단색 여백 자르기, 사진은 JPEG / 도식은 PNG
- 반복 이미지(로고 등)는 한 번만 저장하고 이후에는 `(반복 이미지: …)`로 표시, 64px 미만 아이콘과 빈 이미지는 생략
- EMF · WMF는 PNG로 변환, SmartArt 안의 글자는 텍스트 목록으로 추출
- 다시 변환하면 엔진이 만든 이미지 파일만 지우고 새로 저장합니다.

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
