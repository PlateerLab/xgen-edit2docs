# xgen_edit2docs

**AI 에이전트 네이티브 문서 엔진 — DOCX · XLSX · PPTX. English-first, 한국어는 완전한 1급 지원.**

[![PyPI](https://img.shields.io/pypi/v/xgen_edit2docs)](https://pypi.org/project/xgen_edit2docs/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/xgen_edit2docs/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](./LICENSE)

[English README](./README.md)

`xgen_edit2docs`는 한 줄 의도로 완성된 오피스 문서를 생성하고, 기존 파일을 채팅으로
편집합니다 — Word 보고서, Excel 워크북, PowerPoint 덱 전부 **네이티브 편집
가능한 OOXML**로 (진짜 문단, 진짜 셀, 진짜 차트 — 스크린샷이 아니라).
하나의 엔진, 네 가지 사용 방식: import해서 쓰거나, 에이전트에게 도구로 주거나,
MCP 클라이언트에 연결하거나, 서비스로 띄우거나.

```bash
pip install xgen_edit2docs              # 라이브러리 + 에이전트 도구 + 로컬 MCP
pip install "xgen_edit2docs[server]"    # + 호스팅 멀티테넌트 서비스
```

```python
from xgen_edit2docs import generate_doc, edit_doc

generate_doc("3분기 영업 실적 임원 보고", output="deck.pptx", lang="ko-KR")
r = edit_doc("deck.pptx", "3번 슬라이드 제목을 더 단정적으로 바꿔줘", lang="ko-KR")
print(r.reply)          # 편집기가 무엇을 바꿨는지 한국어로 설명
```

---

## 생태계

| 저장소 | 설명 |
|---|---|
| **[xgen_edit2docs](https://github.com/CocoRoF/xgen_edit2docs)** (이 저장소) | 엔진: 라이브러리 · 에이전트 도구 · MCP · 호스팅 FastAPI 서비스 |
| **[xgen_edit2docs-web](https://github.com/CocoRoF/xgen_edit2docs-web)** | 호스팅 서비스용 웹 스튜디오 — 업로드, 생성, 주소화 프리뷰 위 채팅 편집, 편집 영역 실시간 하이라이트, EN/KO UI. Next.js 15 / React 19 / Tailwind |
| [ppt-master](https://github.com/hugohe3/ppt-master) | PPTX 코어의 upstream 프로젝트 (MIT) — v3.1까지 동기화됨 |
| [edit2ppt](https://github.com/CocoRoF/edit2ppt) | 자매 프로젝트; 덱 파이프라인과 호스팅 서비스의 출처 |

엔진 + 스튜디오의 프로덕션 배포 예시는
[hr_blog2.0](https://github.com/CocoRoF/hr_blog2.0)의 compose 스택에 있습니다 —
`xgen_edit2docs-server/`, `xgen_edit2docs-web/` 서비스 디렉토리가 nginx 뒤에 두 컨테이너를
연결하는 실제 레퍼런스입니다.

---

## 일곱 가지 동사

모든 표면이 같은 일곱 개의 **확장자 디스패치** 동사를 노출합니다 — 파일
확장자가 엔진을 고릅니다. 결정적 동사는 API 키가 필요 없고, 생성형 동사는
BYOK(`api_key=...` 또는 `ANTHROPIC_API_KEY`)입니다.

| 동사 | 하는 일 | LLM? |
|---|---|---|
| `generate_doc` | 의도 (+ 선택: 소스 문서 / PPTX 템플릿) → 완성 문서 | ✳ |
| `edit_doc` | 자연어 편집 1턴; 건드리지 않은 내용은 **바이트 단위로 동일** 유지 | ✳ |
| `preview_doc` | .pptx → 슬라이드별 SVG · .docx/.xlsx → 마크다운 | — |
| `render_doc` | 모든 포맷 → 페이지 **PNG / PDF / SVG** — LibreOffice·서브프로세스 없음 | — |
| `analyze_doc` | `set_doc_text` / `edit_chart`가 쓰는 **정확한 주소**가 담긴 구조 아웃라인 (`charts` 목록 포함) | — |
| `set_doc_text` | 결정적 표적 편집 — **무손실** (차트 / 이미지 / 서식 / 수식 보존) | — |
| `edit_chart` | 네이티브 차트의 **데이터·제목** 결정적 편집 — 차트와 임베디드 워크북을 함께 재작성 | — |

**무손실 편집.** 결정적 편집 동사들은
[xgen_contextifier](https://github.com/CocoRoF/Contextifier)의 raw OOXML 레이어
위에서 동작합니다: 편집이 수술적이고 건드리지 않은 패키지 파트는 바이트
단위로 동일하게 유지되므로, 텍스트 편집 한 번이 차트·피벗·스파크라인·인라인
이미지·수식 캐시를 파괴하는 일이 없습니다. PPTX 채팅 편집(`edit_doc`)도 재생성
슬라이드의 **네이티브 차트/표를 그림으로 납작하게 만들지 않고 보존**합니다.

---

## 1 · Python 라이브러리

### 생성 — 출력 확장자가 엔진을 고릅니다

```python
from xgen_edit2docs import generate_doc

generate_doc("3분기 실적 보고서", output="report.docx", lang="ko-KR")
generate_doc("분기별 매출 정리", output="sales.xlsx", sources=["raw.pdf"], lang="ko-KR")
generate_doc("Q3 영업 결과 임원 보고", output="deck.pptx", lang="ko-KR",
             template="brand.pptx",          # 선택: 사용자 PPTX 템플릿
             deck_mode="template_restyle",   # "new" | "template_restyle" | "template_extend"
             pages=(8, 12))                  # 목표 페이지 범위 (pptx)
```

전체 시그니처: `generate_doc(intent, *, output, api_key=None, sources=None,
template=None, deck_mode="new", pages=(8, 12), lang="en-US", model=...)` →
`GenerateResult(path, page_count, design_spec, warnings)`.

`sources`는 PDF / DOCX / DOC / PPTX / XLSX / HTML / EPUB / IPYNB 경로를 받아
마크다운으로 변환해 작성 LLM의 참고 자료로 전달합니다.

### 편집 — 채팅 1턴, 나머지는 바이트 동일

```python
from xgen_edit2docs import edit_doc

r = edit_doc("report.docx", "진행 사항 섹션에 배포 완료 항목을 추가해줘", lang="ko-KR")
print(r.reply)        # 편집기가 한 일 (요청 언어로)
print(r.operations)   # 적용된 연산, 예: [{"action": "insert_after", ...}]

r = edit_doc("deck.pptx", "이 문서 내용을 반영해서 3번 슬라이드를 고쳐줘",
             sources=["notes.pdf"], lang="ko-KR",
             chat_history=[{"role": "user", "content": "..."},
                           {"role": "assistant", "content": "..."}])
```

플래너 LLM은 문서의 번호 붙은 아웃라인을 보고 **최소한의** 연산을 계획하고,
결정적 엔진이 적용합니다 — 건드리지 않은 문단·셀·슬라이드는 바이트 그대로
살아남습니다. 계획 생성에 실패하면 조용히 넘어가지 않고 응답에서 정직하게
알립니다.

### 검사 & 결정적 편집 (LLM 없음, 키 없음)

```python
from xgen_edit2docs import analyze_doc, set_doc_text, preview_doc, render_doc

info = analyze_doc("report.docx")
# {"format": "docx", "outline": [
#    {"para": 0, "style": "Heading 1", "text": "3분기 보고서"},
#    {"table": 0, "row": 1, "col": 2, "text": "142"}, ...]}   ← 주소

set_doc_text("report.docx", [
    {"para": 0, "new_text": "3분기 최종 보고서"},              # docx: replace / insert_after / delete
])
set_doc_text("sales.xlsx", [
    {"sheet": "매출", "cell": "B3", "value": 142},             # xlsx: set_cell / append_rows / add_sheet
])
set_doc_text("deck.pptx", [
    {"slide": 0, "shape_id": 2, "para": 0, "new_text": "새 제목"},  # pptx
])

preview_doc("deck.pptx", out_dir="previews")   # 슬라이드별 자립 SVG
render_doc("report.docx", to="pdf")            # 페이지 PNG / PDF / 원본 SVG
render_doc("deck.pptx", to="png", dpi=200)     # resvg 래스터 — LibreOffice 불필요
```

생성형 동사의 비동기 버전: `async_generate_doc`, `async_edit_doc`
(이미 실행 중인 이벤트 루프 안에서 사용).

---

## 2 · 에이전트 도구 (function calling)

같은 일곱 동사를 Anthropic tool-use 스키마 + 디스패처로:

```python
import anthropic
from xgen_edit2docs.agent_tools import ANTHROPIC_TOOLS, run_tool

client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=2048,
    tools=ANTHROPIC_TOOLS,
    messages=[{"role": "user", "content": "deck.pptx 3번 슬라이드 제목 고쳐줘"}],
)
for block in msg.content:
    if block.type == "tool_use":
        result = run_tool(block.name, block.input)   # 동기; run_tool_async도 있음
```

---

## 3 · 로컬 MCP 서버 (인프라 제로)

`pip install xgen_edit2docs`에 로컬 파일 대상 stdio 서버 `xgen_edit2docs-mcp`가
포함됩니다 (일곱 동사 전부):

```jsonc
// Claude Desktop / Claude Code / Cursor
{
  "mcpServers": {
    "xgen_edit2docs": {
      "command": "xgen_edit2docs-mcp",
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }   // 생성형 도구만 필요
    }
  }
}
```

그다음 그냥 말하면 됩니다: *"~/decks/roadmap.pptx로 로드맵 10페이지 덱 만들고
PDF로도 렌더링해줘"*.

---

## 4 · 호스팅 서비스

```bash
pip install "xgen_edit2docs[server]"
xgen_edit2docs serve                    # FastAPI :8000 — 스탠드얼론 모드
```

스탠드얼론 모드는 **외부 인프라가 전혀 필요 없습니다**: SQLite + 로컬 파일
스토리지 + 인라인 잡 큐를 첫 부팅 때 자동 구성합니다. 규모가 커지면 환경변수로
Postgres / Redis / S3를 붙이세요.

| REST 엔드포인트 | 용도 |
|---|---|
| `POST /v1/assets` · `GET /v1/assets/{id}` | 문서 업로드 / 조회 (200 MB 제한) |
| `POST /v1/jobs/generate-deck` · `/v1/jobs/edit-deck` | 생성형 잡 큐잉 (3개 포맷 전부) |
| `GET /v1/jobs/{id}` · `GET /v1/jobs/{id}/events` | 잡 상태 · **SSE 진행 스트림** (스테이지 + 주소화 타겟이 담긴 연산별 라이브 편집 이벤트) |
| `POST /v1/preview` | pptx → 슬라이드별 SVG · docx/xlsx → **주소화 HTML** |
| `POST /v1/text-edits` | 결정적 표적 편집 |
| `GET /health` | 생존 확인 + 모드 리포트 |
| `/mcp` · `/mcp-sse` | 같은 동사들의 MCP 노출 (Streamable HTTP / SSE) |

Anthropic 키는 **요청별 BYOK**(`X-Anthropic-API-Key` 헤더) — 절대 저장하지
않습니다. 에러는 이중어로: `message`는 요청의 `Accept-Language`를 따르고
`message_en` / `message_ko`가 항상 함께 옵니다.

주요 환경변수 (접두사 `EDIT2DOCS_`):

| 변수 | 기본값 | 비고 |
|---|---|---|
| `EDIT2DOCS_DEFAULT_LANG` | `en-US` | `ko-KR`로 설정하면 배포 전체가 한국어 기본 |
| `EDIT2DOCS_DATA_DIR` | `/data/xgen_edit2docs` | 스탠드얼론 SQLite + 파일 스토리지 루트 |
| `EDIT2DOCS_DATABASE_URL` | (sqlite) | 예: `postgresql+asyncpg://...` |
| `EDIT2DOCS_REDIS_URL` | (인라인 큐) | arq 워커 큐 활성화 |
| `EDIT2DOCS_S3_*` | (로컬 fs) | S3 호환 스토리지 endpoint / bucket / 키 |
| `EDIT2DOCS_AUTH_DEV_API_KEY` | (익명) | 소규모 배포용 단일 bearer 토큰 |
| `EDIT2DOCS_MAX_UPLOAD_SIZE_BYTES` | 200 MB | 리버스 프록시 설정과 맞추세요 |
| `EDIT2DOCS_MODEL_{PLANNER,WRITER,STRATEGIST,EXECUTOR}` | (요청 모델) | 역할별 모델 오버라이드 — 플래너/작성기 턴을 더 작은 모델로 (호출부 무변경) |
| `EDIT2DOCS_STRATEGIST_SOURCE_CHAR_CAP` | 60000 | 덱 전략가에 투입되는 소스 문서당 상한 (0 = 무제한) |

### 웹 스튜디오

[**xgen_edit2docs-web**](https://github.com/CocoRoF/xgen_edit2docs-web)은 이 서비스의
공식 프론트엔드입니다: 드래그&드롭 업로드, 스테이지별 SSE 진행 표시 생성,
그리고 채팅이 문서를 고치는 동안 캔버스가 **각 연산이 건드리는 문단/셀/
슬라이드를 정확히 하이라이트**하는 공동 편집 스튜디오 (프리뷰 HTML의
`data-e2d-*` 주소, PPTX의 `data-e2p-*` 주소 사용). English-first UI + KO/EN
토글. `EDIT2DOCS_SERVER_INTERNAL_URL` + `EDIT2DOCS_SERVER_API_KEY`로 엔진에
연결합니다.

---

## 포맷별 동작 방식

* **DOCX** — 작성 LLM이 제약된 마크다운 문서를 출력하면 결정적 렌더러
  (python-docx)가 스타일 잡힌 Word로 변환. 편집은 문단 주소 연산(`replace` /
  `insert_after` / `delete`, 표 셀은 `table`/`row`/`col`). 호스팅 프리뷰는
  네이티브 *주소화* HTML — 모든 문단에 `data-e2d-para`, 모든 셀에
  `data-e2d-cell`(아웃라인·라이브 편집 스트림과 같은 주소) + 병합셀, 정렬,
  색상, 이미지, 각주, 페이지 나눔.
* **XLSX** — 설계 LLM이 YAML *시트 스펙*(시트/헤더/행/숫자서식, 수식 허용)을
  출력하면 openpyxl이 스타일 잡힌 시트로 렌더링. 편집은 `set_cell` /
  `append_rows` / `add_sheet` (+ 낡은 값 가드). 호스팅 프리뷰는 스프레드시트
  그리드(열 문자, 행 번호, 병합 범위, 수식 캐시값) — 모든 셀에
  `data-e2d-cell="B3"` (정확히 `set_cell`이 받는 주소).
* **PPTX** — 완전한 다단계 파이프라인: 전략가 → 페이지별 SVG → 네이티브
  DrawingML, 사용자 PPTX 템플릿(restyle/extend), 슬라이드 재구성 채팅 편집,
  표 셀 포함 문단 단위 텍스트 편집, 선택적 Edge-TTS 나레이션. 내보낸 텍스트는
  **문단 병합** 상태(줄 단위 박스가 아니라 진짜 문단으로 편집됨).

### 네이티브 차트 & 표 (PPTX)

`data-pptx-native="chart|table"` 마커가 붙은 SVG 그룹은 그려진 도형이 아니라
**진짜 편집 가능한 PowerPoint 객체**로 내보내집니다 — 임베디드 Excel 워크북이
달린 차트 XML 파트(PowerPoint에서 더블클릭해 데이터 편집 가능) 또는 네이티브
`<a:tbl>` 표:

```xml
<g id="sales_chart" data-pptx-native="chart">
  <metadata data-pptx-native="chart">
    { "name": "sales_chart",
      "x": 125, "y": 141, "width": 1000, "height": 440,
      "type": "bar",
      "categories": ["Q1", "Q2", "Q3"],
      "series": [{ "name": "매출", "values": [120, 135, 150] }] }
  </metadata>
  <!-- 폴백 도형 — 네이티브 내보내기 꺼짐 시 사용 -->
</g>
```

`ExportRequest(native_objects=True)`로 opt-in (`tools/export.py`). 지원 차트:
bar / column / line / area / pie / doughnut / of-pie / radar (클래식),
scatter / bubble (XY), box-whisker / funnel / histogram / pareto / sunburst /
treemap / waterfall (chartEx). 품질 체커가 내보내기 전에 마커 페이로드를
검증합니다.

모든 LLM 플래너는 같은 계약을 따릅니다: 펜스된 `reply` + `edit_plan` 블록,
형식 리마인더와 함께 1회 재시도, 계획 실패 시 조용한 no-op 대신 정직한 응답.

---

## 언어

기본은 영어(`lang="en-US"`)지만 **한국어는 뒷전이 아니라 1급 시민**입니다 —
한글 인지 텍스트 폭 계산, 실제 문자 스크립트를 감지한 런 단위 OOXML `lang`
속성, 한국어 폰트 스택(Pretendard/맑은고딕), 완전한 한국어 메시지 카탈로그,
로컬라이즈된 채팅 응답과 라이브 편집 라벨. 호출 단위는 `lang="ko-KR"`,
요청 단위는 `Accept-Language: ko-KR`, 배포 전체는
`EDIT2DOCS_DEFAULT_LANG=ko-KR`로 전환합니다. zh-CN / zh-TW / ja-JP도 같은
스크립트 감지·폰트 스택 처리를 받습니다.

---

## 개발

```bash
git clone https://github.com/CocoRoF/xgen_edit2docs && cd xgen_edit2docs
uv venv .venv && uv pip install -e ".[server,dev]"
.venv/bin/python -m pytest tests/          # 769개 테스트
.venv/bin/python -m ruff check src/xgen_edit2docs --exclude src/xgen_edit2docs/core
```

## 버전 이력

| 버전 | 주요 내용 |
|---|---|
| **v0.7.0** | upstream 동기화 (ppt-master v2.7 → v3.1, 3웨이브): **네이티브 차트/표 내보내기**, 문단 병합 편집성, PowerPoint 복구 프롬프트 해결, 체커 강화 · **English-first 전환** (한국어 완전 지원) |
| v0.5–0.6 | `render_doc` — 3개 포맷 전부 PNG/PDF/SVG 네이티브 페이지 렌더링 (resvg + PyMuPDF, LibreOffice 불필요) |
| v0.4.0 | 주소화 네이티브 프리뷰 (`data-e2d-*`) — 프리뷰·아웃라인·에디터가 하나의 주소 체계 공유 |
| v0.3.0 | 라이브 편집 스트리밍 — 주소화 타겟이 담긴 연산별 SSE 이벤트 |
| v0.2.x | 멀티포맷 호스팅 API + 전 포맷 하드닝 |
| **v0.9.0** | **토큰 최적화** — 프롬프트 캐시 재구성(편집 재시도가 캐시 프리픽스를 ~10× 저렴하게 읽음, 실행기 spec_lock 페이지당 재전송 제거), 팬아웃 캐시 웜업, 무제한 입력 캡(전략가 소스·편집 아웃라인 윈도잉), 재시도 심각도 티어링, 역할별 모델 티어링, 스트리밍, 캐시 회계·스테이지별 비용 |
| v0.8.0 | **무손실 편집** — xgen_contextifier raw OOXML 레이어 위: set_doc_text/edit_doc가 차트·이미지·스파크라인·서식·수식 캐시를 파괴하지 않음; PPTX 채팅편집 네이티브 차트/표 보존; 신규 **`edit_chart`** 동사(데이터+제목, 임베디드 워크북 동기화) |
| v0.1.0 | 멀티포맷 엔진: DOCX/XLSX/PPTX 핵심 동사 |

## 라이선스

[Apache-2.0](./LICENSE). `src/xgen_edit2docs/core/`의 PPTX 코어는
[ppt-master](https://github.com/hugohe3/ppt-master)(MIT, © Hugo He)에서
[edit2ppt](https://github.com/CocoRoF/edit2ppt)를 거쳐 파생됐으며 upstream
v3.1까지 동기화 유지 중입니다. 해당 부분의 원본 MIT 조항은
[NOTICE](./NOTICE)와 [LICENSE.ppt-master.MIT](./LICENSE.ppt-master.MIT)에
보존되어 있습니다.
