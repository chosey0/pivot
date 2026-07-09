# 패키지 구조 설계

`pivot/` 파이썬 패키지(파이프라인 라이브러리)와 `scripts/`(얇은 실행 CLI)를 분리한다.
서브패키지는 파이프라인 단계 순서를 따르며, **구현 순서대로 하나씩 추가한다**
(빈 placeholder를 미리 만들지 않는다).

## 목표 구조

```
pivot/                        # 저장소 루트
├── pyproject.toml            # uv + hatchling, broker-modules git 의존성
├── pivot/                    # 파이썬 패키지
│   ├── __init__.py
│   ├── config.py             # 파이프라인/실험 설정 (백로그 C: config 기반 실험 관리)
│   ├── ingestion/            # ① 데이터 수집 — docs/03
│   │   ├── fetch.py          #   broker-modules 비동기 조회 (rate limit 고려)
│   │   ├── schema.py         #   ChartBar → 표준 DataFrame 변환 + 스키마 검증
│   │   ├── indicators.py     #   이평선 5/20/120 직접 계산
│   │   └── cache.py          #   parquet 캐시 입출력
│   ├── labeling/             # ② 프랙탈 라벨링
│   │   └── fractal.py        #   calc_fractal + 옵션 필터(정배열/유동성, B5)
│   ├── dataset/              # ③ 시퀀스 샘플 생성/로딩
│   │   ├── build.py          #   샘플 생성 (low/high 루프 통합 A7, float 직렬화 A1, Time 제외 A2)
│   │   ├── transforms.py     #   스케일링 공용 모듈 — 학습·실시간 추론 공유 (A4)
│   │   └── loader.py         #   torch Dataset + collate (마스킹/패딩 A3)
│   ├── models/               # ④ 모델
│   │   └── cnn1d.py          #   재현 베이스라인 (B1 비교 실험의 기준점)
│   ├── training/             # ⑤ 학습
│   │   ├── train.py          #   학습 루프 (종목 단위 split A5, 안정화 B6)
│   │   └── metrics.py        #   클래스별 P/R/F1, confusion matrix (A6)
│   └── evaluation/           # ⑥ 시각화 평가 (viz extra 필요)
│       └── plot.py           #   finplot 캔들 + 프랙탈 + 예측 오버레이
├── scripts/                  # 실행 스크립트 — 파이프라인 함수 호출만, 로직 없음
│   ├── fetch_candles.py      #   ① 심볼 리스트 → 캐시 저장
│   ├── build_dataset.py      #   ②+③ 캐시 → 라벨링 → 데이터셋 생성
│   └── train.py              #   ⑤ 데이터셋 → 학습 → 체크포인트
├── data/                     # git 미추적
│   ├── raw/                  #   수집 캐시: {broker}/{interval}/{symbol}.parquet
│   └── processed/            #   생성된 데이터셋 + 메타 json
├── models/saved/             # git 미추적 — 체크포인트, 학습 히스토리 json
└── docs/
```

## 설계 원칙

- **패키지 = 라이브러리, 스크립트 = 진입점.** `scripts/`는 인자 파싱과 함수 호출만 한다.
  구 프로젝트에서 `scripts/data/dataset.py`에 파이프라인 로직이 살던 구조를 뒤집는다.
- **단계 간 결합은 데이터 계약(표준 DataFrame 스키마)으로만.** ingestion의 출력
  (`Time` 인덱스 + `Open/High/Low/Close/Volume/Amount/5/20/120`)이 labeling 이후의 입력.
  브로커 의존성은 ingestion 안에 가둔다.
- **`transforms.py`는 torch 비의존.** 실시간 추론에서도 같은 스케일링을 재현해야 하므로(A4)
  numpy/pandas 수준으로 유지하고, torch 의존은 `loader.py`·`models/`·`training/`에만 둔다
  (core 설치만으로 수집~라벨링 사용 가능, `train` extra로 학습).
- **실시간 추론(구 PyQt5 앱)은 이 구조에 아직 없다.** 모델 성능 검증 뒤 별도
  서브패키지(`pivot/realtime/`)로 설계한다 (백로그 C).

## 의존성 구분 (pyproject)

| 구분 | 패키지 | 용도 |
|---|---|---|
| core | broker-modules, pandas, pyarrow | 수집 ①~③ (transforms까지) |
| `train` extra | torch, scikit-learn | 로더/모델/학습 ③(loader)~⑤ |
| `viz` extra | finplot | 시각화 평가 ⑥ |

실행 예: `uv run --env-file .env scripts/fetch_candles.py`
