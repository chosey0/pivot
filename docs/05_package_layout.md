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
│   ├── symbols/              #   KIS 종목마스터 정규화 + Supabase domestic_master 업서트/검색
│   ├── labeling/             # ② 프랙탈 라벨링
│   │   └── fractal.py        #   calc_fractal + 옵션 필터(정배열/유동성, B5) + 라벨 모드(B2)
│   ├── dataset/              # ③ 시퀀스 샘플 생성/로딩
│   │   ├── build.py          #   샘플 생성 (low/high 루프 통합 A7, float 직렬화 A1, Time 제외 A2)
│   │   ├── transforms.py     #   스케일링 공용 모듈 — 학습·실시간 추론 공유 (A4), torch 비의존
│   │   ├── storage.py        #   Supabase dataset 메타데이터 + private parquet shard 저장 계약
│   │   └── loader.py         #   Storage shard 로딩 + torch Dataset/collate (마스킹/패딩 A3)
│   ├── diagnostics/          #   데이터 품질 진단 — raw cache / preset preview / dataset 리포트
│   │   └── quality.py        #   timestamp, OHLC, MA NaN, 라벨 분포, split 누수 검사
│   ├── models/               # ④ 모델
│   │   └── cnn1d.py          #   재현 베이스라인 (B1 비교 실험의 기준점)
│   ├── training/             # ⑤ 학습/평가
│   │   ├── train.py          #   학습 루프 (종목 단위 split A5, 안정화 B6)
│   │   ├── metrics.py        #   클래스별 P/R/F1, confusion matrix (A6)
│   │   ├── runs.py           #   Supabase run/epoch/평가 메타데이터 + checkpoint 관리
│   │   └── evaluate.py       #   종목 히스토리에 모델 적용 → 실제 라벨 vs 예측 (웹 차트 검증용)
│   └── realtime/             # ⑥ 실시간 추론 (M5)
│       ├── aggregate.py      #   체결 틱 → 봉 집계 (현재 봉 갱신/마감)
│       └── infer.py          #   체크포인트 로드 + transforms 재사용 시퀀스 구성/판정
├── server/                   # FastAPI 앱 — pivot 패키지 호출만, 도메인 로직 없음
│   ├── main.py               #   앱 조립, web 빌드 정적 서빙
│   ├── routers/              #   symbols, watchlist, ingest, preprocess, presets, datasets, diagnostics, runs, live
│   ├── jobs.py               #   장기 작업(수집/일괄 전처리/학습) 상태 + SSE, 학습은 별도 프로세스
│   └── live.py               #   증권사 WS 구독 관리 + 브라우저 WS 브로드캐스트
├── web/                      # Vite + React + TS — docs/04 §5·§6
│   └── src/                  #   api/, components/chart/, pages/{Watchlist,Lab,Datasets,Diagnostics,Training,Live}
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
  브로커 의존성은 ingestion과 `server/live.py` 안에 가둔다.
- **`transforms.py`는 torch 비의존.** 실시간 추론에서도 같은 스케일링을 재현해야 하므로(A4)
  numpy/pandas 수준으로 유지하고, torch 의존은 `loader.py`·`models/`·`training/`·
  `realtime/infer.py`에만 둔다.
- **모든 로직은 타임프레임 무관(agnostic).** 타임프레임은 프리셋의 값일 뿐,
  ingestion 이후 단계는 봉의 종류를 몰라야 한다.
- **학습 관련 데이터의 단일 원본은 Supabase다.** 프리셋, job, 데이터셋/종목/분할 메타데이터,
  진단 리포트, run/epoch/평가 메타데이터는 Postgres에 저장한다. 데이터셋 shard와 모델
  artifact는 private Storage에 저장하며 로컬 파일은 실행 중 임시 캐시로만 사용한다.
- **Supabase 접근은 저장소 경계에서만 수행한다.** `pivot/`의 저장 인터페이스가 메타데이터와
  artifact 계약을 소유하고, `server/`는 서버 전용 키를 주입해 호출만 한다. 브라우저에는
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

개발 실행: `uv run --env-file .env uvicorn server.main:app --reload` + `web/`에서 `npm run dev`
(Vite proxy `/api` → 8000). 배포(로컬 사용)는 `vite build` 산출물을 FastAPI가 정적 서빙.
