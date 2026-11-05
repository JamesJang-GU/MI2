# MI2 — 국제운송 지표 자동 크롤링 + 통합 대시보드

7개 지표 모듈(항공·선사·포워더·컨테이너선대·거시·유가·해운)을
**매일 자정(KST 00:00)** GitHub Actions가 자동으로 크롤링하고,
결과를 **GitHub Pages 정적 대시보드**로 PC/모바일에서 조회하는 구조입니다.

## 구조

```
MI2/
├── .github/workflows/main.yml   # 매일 KST 00:00 크롤링 → 결과 자동 커밋
├── run_all.py                   # ① 7개 모듈 순차 실행 ② summary.json/status.json 생성
├── index.html                   # 통합 포털 대시보드 (PC/모바일 반응형)
├── requirements.txt             # Actions 설치용 의존성
├── summary.json / status.json   # run_all.py가 생성 (커밋됨)
└── <모듈>/                      # 모듈별 폴더 (기존 구조 유지)
    ├── <모듈>.py                # 크롤러 + 정적 페이지 생성기 (--cron 모드)
    ├── <모듈>.db                # SQLite 이력 (매일 커밋되어 누적)
    └── index.html               # 모듈별 상세 대시보드 (실행 시 생성)
```

| 모듈 | 지표 | 크롤링 방식 | 상세 페이지 |
|---|---|---|---|
| `sea` | SCFI·BDI (해운운임) | Selenium + KSG 계정 | 포털 카드 |
| `air` | TAC/Baltic 항공운임 | requests | `air/index.html` |
| `alphaliner` | Top100 선사 선대 | Selenium | `alphaliner/index.html` |
| `forwarders` | A&A Top25 포워더 | requests+BS4 | `forwarders/index.html` |
| `linerlytica` | 컨테이너 선대 현황 | Playwright | `linerlytica/index.html` |
| `macro` | GDP·물가·GSCPI | requests | `macro/index.html` |
| `oil` | VLSFO·원유가 | requests/Selenium | `oil/index.html` |

## 동작 흐름 (하루 1회)

1. `cron: 0 15 * * *` (UTC 15:00 = **KST 00:00**)에 Actions 시작
   (Actions > daily-crawl > Run workflow 로 수동 실행도 가능)
2. `python run_all.py` → 각 모듈을 헤드리스 모드(`--cron` 등)로 순차 실행
   - 한 모듈이 실패해도 나머지는 계속 실행, 결과는 `status.json`에 기록
3. 모듈별 `.db` 갱신 + 모듈별 `index.html` 재생성
4. `run_all.py`가 DB에서 포털용 `summary.json` 추출
5. 변경분(`.db`, `.json`, `.html`)을 자동 커밋·푸시 → GitHub Pages 반영

## 최초 1회 설정 (GitHub 웹에서)

1. **Pages 활성화**: Settings → Pages → Source: *Deploy from a branch*
   → Branch: `main`, 폴더 `/ (root)` → Save
   → 이후 `https://<사용자>.github.io/MI2/` 가 포털 대시보드
2. **Secrets 등록** (sea 모듈용): Settings → Secrets and variables → Actions
   → `KSG_ID`, `KSG_PW` 추가 (미등록 시 sea만 실패하고 나머지는 정상 동작)
3. PR 머지 후 스케줄 크롤링은 `main` 브랜치에서 동작합니다
   (스케줄 이벤트는 기본 브랜치에서만 실행되기 때문)

## 로컬 실행 (VS Code)

```bash
pip install -r requirements.txt
python run_all.py                # 전체 크롤링 + 요약 생성
python run_all.py sea oil        # 일부 모듈만
python run_all.py --summary-only # 크롤링 없이 포털 데이터만 재생성
python -m http.server 8000       # 포털 확인 → http://localhost:8000
```

모듈 단독 실행도 기존과 동일합니다 (예: `python sea/sea.py` = 로컬 Flask 서버,
`python macro/macro.py --cron` = 정적 생성).

## 새로운 모듈 추가 방법 (3줄)

1. `<모듈>/<모듈>.py` + `<모듈>.db` 폴더 생성 (헤드리스 모드 필수)
2. `run_all.py`의 `MODULES` 리스트와 `SUMMARIZERS`에 추출기 추가
3. 끝 — 워크플로우/포털은 자동 반영

## 참고

- `.db`는 매일 커밋되어 이력이 누적됩니다. 저장소가 무거워지면 이후 data 브랜치
  분리 등을 검토할 수 있습니다 (지금은 단순함 우선).
- SQLite WAL 사이드카(`*.db-wal`, `*.db-shm`)와 로그는 `.gitignore` 처리됨.
