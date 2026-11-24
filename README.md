# 릴스 수집기 (로컬 버전)

`docs/instagram_reels_collector_PRD.md` 를 로컬 PC 에서 돌아가게 구현한 것입니다.

**범위 조정:** 수집은 키워드 화면의 "수집 실행" 버튼으로만 시작합니다. 다음 PRD 항목은 구현하지 않습니다.
- 주기 실행·크론 수집 (FR-03)
- 순위 변화 비교 (US-04)
- 모바일 일치도 측정 (G3 측정, FR-50~52, US-07, `mobile_baselines`)
- 실패한 영상 다운로드 자동 재시도 (FR-35)
- 주기적 알림 점검 — "보관·알림" 화면의 "지금 점검" 버튼으로만 확인 (US-06 의 자동 알림)
- 보관 기한 자동 삭제 — "보관 정리 지금 실행" 버튼으로만 정리 (FR-60/61 의 자동 삭제)

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # 필요한 값 입력
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000 을 엽니다. 영상 검증에는 `ffprobe`(ffmpeg)가 PATH 에 있어야 합니다.

테스트: `.\.venv\Scripts\python -m unittest discover tests`

## 제공사 설정 (`.env`)

| 값 | 설명 |
| --- | --- |
| `PRIMARY_PROVIDER` | `mock`(키 없이 시험) / `hikerapi` / `apify` |
| `FALLBACK_PROVIDER` | 장애·410 시 전환할 제공사. mock 시험 시 `mock_backup` |
| `HIKERAPI_KEY` | HikerAPI 키 (헤더 `x-access-key`) |
| `HIKERAPI_SEARCH_ENDPOINT` | 기본 `/gql/topsearch`. 아래 주의 참고 |
| `APIFY_TOKEN` | Apify 토큰 (무료 플랜은 실행당 영상 링크 3개만 줌) |
| `*_COST_*`, `BASELINE_COST_PER_KEYWORD` | 비용 집계·알림 기준. PoC 실측 후 조정 |
| `APP_USER` / `APP_PASSWORD` | 설정하면 화면·영상 접근에 HTTP Basic 인증 (NFR-07) |
| `ALERT_WEBHOOK_URL` | Slack 호환 웹훅. 비우면 화면에만 알림 |

### ⚠ HikerAPI 엔드포인트 변경 (2026-11-24 OpenAPI v1.8.4 확인)
- PRD 의 `/v2/fbsearch/reels` 는 **deprecated — "Closed by Instagram since 2026-11-13"**. 대안은 `/gql/topsearch` 이며
  사람·릴스가 섞여 나오므로 어댑터가 릴스만 골라냅니다. 응답 필드명은 PoC 에서 확인이 필요합니다(파서는 미디어 객체를 재귀로 찾음).
- 해시태그 `/v2/hashtag/medias/top|recent` 도 폐기. 인기 탭은 `/g2/hashtag/medias` 로 대체, **최신 탭은 대체 없음**.
- 서버 시작 시 및 상단 배지에서 OpenAPI 헬스체크 결과(엔드포인트 존재·deprecated 여부)를 보여줍니다.

### Mock 제공사 시험 규칙
`다이어트` 포함 → 차단(screened)·해시태그 수집 / `소진` 포함 → 45개에서 검색 소진 후 해시태그 보충 /
`410` 포함 → 410 발생 후 보조 제공사 전환. 일부 영상 URL 은 만료(403)를 일부러 내서 재발급을 시험하고, 약 2%는 오류(500)를 내서 실패 처리를 시험합니다(재시도 없음).

## PRD 대비 로컬 구성

| PRD | 로컬 구현 | 운영 전환 |
| --- | --- | --- |
| PostgreSQL | SQLite (`data/reels.db`, WAL) | `DATABASE_URL=postgresql+psycopg://...` + `psycopg` 설치 |
| Redis + Celery | 프로세스 내 스레드 풀 (검색 2 / 다운로드 8) | 워커 함수(`execute_run`, `download_file`)를 Celery task 로 감싸면 됨 |
| S3/MinIO | `storage/reels/{media_pk}/video.mp4`, `thumbnail.jpg` | 저장 키 동일 |
| cron | 없음. 내부적으로 실행 종료 판정(5초)만 돌고, 알림 점검·보관 정리는 화면 버튼으로 수동 실행 | |
| Metabase/Grafana | 내장 대시보드 | |

## 요구사항 대응

- **키워드·실행** FR-01, 02, 04: 키워드 화면(목표 수량·로케일, 수동 실행), 실행 목록/상세(상태·진행 수량 자동 갱신)
- **검색** FR-10~15: `app/services/collector.py` — 커서 이동, 목표 도달·20페이지에서 중단, 원본 응답 저장, 소진·차단 시 해시태그 보충(출처 구분), 장애·410 시 보조 제공사 전환
- **정규화** FR-20~23: `ReelItem` 공통 형식, `media_pk` 중복 제거(첫 순위 유지), 사진·캐러셀 제외, 최신 지표 갱신 + `run_results.metrics_snapshot`
- **다운로드** FR-30~34, 36: `app/services/downloader.py` — 페이지마다 즉시 큐 등록, 스트리밍 + SHA-256, ffprobe 검증, 저장된 것은 재다운로드 안 함, 403/410·3시간 경과 시 URL 재발급. 실패한 다운로드는 재시도하지 않고, 같은 키워드를 다시 실행하면 새 URL 로 다시 받음
- **조회** FR-40~42: CSV(Excel 한글 BOM), 영상 재생, 대시보드(실행 현황·실패 사유·비용)
- **보관** FR-60~62: 90일 영상 / 1년 메타데이터 / 30일 원본 — "보관 정리 지금 실행" 버튼으로 정리(자동 삭제 없음), 연장 요청·승인, 재발급 시 404 이면 `is_available=FALSE`
- **완료 판정** NFR-01/04: 고유 100개 + 영상 95개 이상이면 `done`, 15분 초과·미달이면 `partial`

PRD 에 없던 추가 테이블·컬럼: `alerts`, `retention_extensions`, `collection_runs.cost_usd/deadline_at/...`, `media_files.provider/next_attempt_at/refresh_count`.

## 알려진 한계
- 원본 삭제(`is_available`)는 URL 재발급 시점에만 감지합니다. 주기적인 전수 확인은 호출 비용 때문에 넣지 않았습니다.
- Apify 어댑터는 단건 URL 재발급을 지원하지 않아, 만료 시 HikerAPI 키가 있으면 HikerAPI 로 재발급합니다.
- 법무 검토(PRD §8) 전에는 실제 제공사로 수집하지 마십시오.
