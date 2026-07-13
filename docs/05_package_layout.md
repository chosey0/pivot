# 패키지/저장소 구조 설계

`pivot/` 파이썬 패키지(순수 도메인 라이브러리)와 진입점을 분리한다.
웹 애플리케이션 설계([04_webapp_design.md](04_webapp_design.md)) 기준으로 진입점은
`server/`(FastAPI)와 `web/`(React)이다. 서브패키지는 파이프라인 단계 순서를 따르며,
**구현 순서대로 하나씩 추가한다** (빈 placeholder를 미리 만들지 않는다).

## 목표 구조

```
pivot/                        # 저장소 루트
├── pyproject.toml            # uv + hatchling, broker-modules git 의존성
├── pivot/                    # 파이썬 패키지 — 순수 도메인, 웹 비의존
│   ├── __init__.py
│   ├── config.py             # 타임프레임/프리셋/run 설정 스키마 (pydantic) — 보조지표 표시와 학습 피처 선택 포함
│   ├── ingestion/            # ① 데이터 수집 — docs/03
│   │   ├── fetch.py          #   broker-modules 비동기 조회 (타임프레임: day/min{N}/tick{N}, rate limit)
│   │   ├── schema.py         #   ChartBar → 표준 DataFrame 변환 + 스키마 검증
│   │   ├── indicators.py     #   이평선 직접 계산 (ma_source: self | daily)
│   │   └── cache.py          #   parquet 캐시 입출력 (증분 갱신)
│   ├── env.py                #   저장소 루트 .env 로더 (프로세스 환경변수 우선)
│   ├── symbols/              #   KIS 종목마스터 정규화 + Supabase domestic_master 업서트/검색
│   ├── storage/              #   Supabase 학습 데이터 저장소 경계 — docs/06 계약 소유
│   │   └── runs.py           #   run/epoch/evaluation/artifact 메타데이터 repository
│   │   ├── supabase.py       #   PostgREST(메타데이터)·Storage(객체) 클라이언트 분리
│   │   ├── presets.py        #   training_presets repository (버전 증가·archive·검증)
│   │   ├── jobs.py           #   jobs/job_events repository (상태 전이 강제)
│   │   ├── datasets.py       #   datasets/dataset_symbols/dataset_shards repository
│   │   ├── diagnostics.py    #   diagnostic_reports repository (불변 리포트)
│   │   └── lifecycle.py      #   데이터셋 삭제(객체→메타 순서) + orphan/stale 정리
│   ├── cleaning/             # ② 원천 불변 품질 경계 분석
│   │   └── kronos.py         #   Kronos Appendix B 적응형 세그먼트 분석 (off/report_only/filter)
│   ├── labeling/             # ③ 프랙탈 라벨링
│   │   └── fractal.py        #   calc_fractal + 옵션 필터(정배열/유동성, B5) + 라벨 모드(B2)
│   ├── dataset/              # ④ 시퀀스 샘플 생성/로딩
│   │   ├── build.py          #   샘플 생성 (low/high 루프 통합 A7, float 직렬화 A1, Time 제외 A2)
│   │   ├── shards.py         #   샘플 → parquet shard 직렬화 (SHA-256, 50MiB 미만 분할)
│   │   ├── batch.py          #   일괄 전처리 파이프라인 (run_preprocess 재사용, split 배정, 업로드 검증, 협조적 취소)
│   │   ├── samples.py        #   샘플 브라우저 접근 (전역 순번 인덱스, 해시 검증 다운로드 + tmp 캐시)
│   │   ├── transforms.py     #   스케일링 공용 모듈 — 학습·실시간 추론 공유 (A4), torch 비의존
│   │   └── loader.py         #   Storage shard 로딩 + torch Dataset/collate (마스킹/패딩 A3)
│   ├── diagnostics/          #   데이터 품질 진단 — raw cache / preset preview / dataset 리포트
│   │   └── quality.py        #   timestamp, OHLC, MA NaN, 라벨 분포, split 누수 검사
│   ├── models/               # ⑤ 모델
│   │   └── cnn1d.py          #   재현 베이스라인 (B1 비교 실험의 기준점)
│   ├── training/             # ⑥ 학습/평가
│   │   ├── train.py          #   학습 루프 (종목 단위 split A5, 안정화 B6)
│   │   ├── metrics.py        #   클래스별 P/R/F1, confusion matrix (A6)
│   │   ├── runs.py           #   학습 run orchestration + 검증 checkpoint 업로드
│   │   └── evaluate.py       #   종목 히스토리에 모델 적용 → 실제 라벨 vs 예측 (웹 차트 검증용)
│   └── realtime/             # ⑦ 실시간 추론 (M5)
│       ├── aggregate.py      #   broker-neutral 체결 → day/min/tick 봉 집계 (현재 봉 갱신/마감)
│       └── infer.py          #   체크포인트 로드 + transforms 재사용 시퀀스 구성/판정
├── server/                   # FastAPI 앱 — pivot 패키지 호출만, 도메인 로직 없음
│   ├── main.py               #   앱 조립, web 빌드 정적 서빙
│   ├── routers/              #   symbols, watchlist, ingest, preprocess, presets, datasets, diagnostics, runs, live
│   ├── jobs.py               #   장기 작업(수집/일괄 전처리/학습) 상태 + SSE, 학습은 별도 프로세스
│   └── live.py               #   Kiwoom 단일 WS session/0B 구독 + gap 보정 + 브라우저 WS 브로드캐스트
├── web/                      # Vite + React + TS — docs/04 §5·§6
│   └── src/                  #   App.tsx는 탭 셸만. api/, lib/(format·timeframe 공용 유틸),
│                             #   components/{chart,indicators}/, pages/{Watchlist,Lab,Datasets,Diagnostics,...}
├── data/                     # git 미추적 — 로컬 운영 데이터/임시 캐시, docs/04 §4
│   ├── raw/                  #   수집 캐시: {broker}/{timeframe}/{symbol}.parquet
│   ├── meta/                 #   watchlist.json
│   └── tmp/                  #   Storage 업로드/다운로드 중 재생성 가능한 작업 캐시
├── supabase/
│   └── migrations/          #   종목마스터 + 학습 메타데이터/Storage bucket DDL
└── docs/
```

## 설계 원칙

- **패키지 = 라이브러리, server/web = 진입점.** `server/`는 인자 검증·job 관리·직렬화만 하고
  도메인 로직은 전부 `pivot/`에 둔다. 구 프로젝트에서 `scripts/data/dataset.py`에
  파이프라인 로직이 살던 구조를 뒤집는다. (필요해지면 `scripts/`에 얇은 CLI 래퍼를
  추가할 수 있지만, 반드시 `pivot/` 함수 호출만 한다)
- **단건 미리보기(Lab)와 일괄 처리(batch)는 같은 `pivot/` 함수를 호출한다.**
  호출자별로 파이프라인을 복제하지 않는다.
- **차트 보조지표와 학습 피처는 분리한다.** Watchlist/Lab에서 lightweight-charts series로 표시한
  보조지표라도 프리셋 `features`에서 제외하면 데이터셋에는 들어가지 않는다. 표시하지 않은
  보조지표도 `features`에 포함하면 전처리/학습 피처로 사용한다.
- **데이터 진단은 읽기 전용 품질 게이트다.** 원천 캐시, 프리셋 적용 결과, 데이터셋을 검사해
  경고/실패를 보고하지만 데이터를 자동 수정하지 않는다. 수정은 수집 갱신, 프리셋 조정,
  데이터셋 재생성으로 명시적으로 수행한다.
- **단계 간 결합은 데이터 계약(표준 DataFrame 스키마)으로만.** ingestion의 출력
  (`Time` 인덱스 + `Open/High/Low/Close/Volume/Amount` + 이평선 컬럼)이 labeling 이후의 입력.
  브로커 의존성은 ingestion과 `server/live.py` 안에 가둔다. M5의 실시간 브로커는
  `brokers.kiwoom`으로 고정하고 `pivot/realtime/`에는 정규화된 체결 값만 전달한다.
- **`transforms.py`는 torch 비의존.** 실시간 추론에서도 같은 스케일링을 재현해야 하므로(A4)
  numpy/pandas 수준으로 유지하고, torch 의존은 `loader.py`·`models/`·`training/`·
  `realtime/infer.py`에만 둔다.
- **모든 로직은 타임프레임 무관(agnostic).** 타임프레임은 프리셋의 값일 뿐,
  ingestion 이후 단계는 봉의 종류를 몰라야 한다.
- **학습 관련 데이터의 단일 원본은 Supabase다.** 프리셋, job, 데이터셋/종목/분할 메타데이터,
  진단 리포트, run/epoch/평가 메타데이터는 Postgres에 저장한다. 데이터셋 shard와 모델
  artifact는 private Storage에 저장하며 로컬 파일은 실행 중 임시 캐시로만 사용한다.
- **Supabase 접근은 저장소 경계에서만 수행한다.** `pivot/storage/`가 메타데이터와
  artifact 계약을 소유하고 (초기 설계의 `dataset/storage.py`를 프리셋·job까지 포괄하는
  전용 서브패키지로 확장), `server/`는 서버 전용 키를 주입해 호출만 한다. 브라우저에는
  secret/service-role 키나 private object URL을 노출하지 않는다.
- 시각화 평가는 웹 차트(Training 탭 차트 검증)로 수행한다 — 초기 계획의 finplot
  기반 `evaluation/plot.py`는 폐기 (`viz` extra 불필요).

## 의존성 구분 (pyproject)

| 구분 | 패키지 | 용도 |
|---|---|---|
| core | broker-modules, pandas, pyarrow, pydantic | 수집 ①~③ (transforms까지) |
| core | httpx | Supabase REST 종목마스터 및 학습 메타데이터/Storage 접근 |
| `server` extra | fastapi, uvicorn | 웹 서버 (SSE/WebSocket은 fastapi로 처리) |
| `train` extra | torch, scikit-learn | 로더/모델/학습 ③(loader)~⑤, 실시간 추론 ⑥ |

개발 실행은 운영체제와 무관하게 저장소 루트에서
`uv run --extra server --extra train python scripts/dev.py all`을 사용한다.
API와 웹을 분리해서 실행할 때는 마지막 인자를 각각 `api`, `web`으로 바꾼다.
공통 런처가 Windows의 `npm.cmd`와 macOS/Linux의 `npm` 차이를 처리하고,
Vite proxy `/api`와 `/ws`는 선택한 API 포트를 따른다. 배포(로컬 사용)는
`vite build` 산출물을 FastAPI가 정적 서빙.
