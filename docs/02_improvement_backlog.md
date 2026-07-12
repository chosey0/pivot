# 개선 백로그

구 Fractal 코드를 리뷰하며 발견한 문제점과 개선 아이디어.
**전처리 방법(프랙탈 라벨링 방식 자체)은 유지**하되, 구현 품질 문제와 방법 개선 실험을 구분한다.

## A. 버그/품질 문제 — 재구현 시 바로 고칠 것

### A1. 이평선 값의 int64 캐스팅으로 정밀도 손실
`create_dataset()`이 `use_cols` 전체(`20`, `120` 포함)를 `int64`로 캐스팅한다.
float인 이평선 값이 소수점 아래를 잃는다. → 피처는 float으로 직렬화.

### A2. Time 컬럼이 불필요하게 저장/스케일링됨
Time을 epoch ns 정수로 함께 직렬화하고, collate에서 스케일링까지 한 뒤 버린다.
거대한 정수가 StandardScaler 통계에는 안 섞이지만(컬럼별 독립) 저장 낭비 + 매 epoch 낭비 연산.
→ 저장 시점에 Time 제외, 또는 인덱스로만 유지.

### A3. 패딩 값 100이 표준화된 데이터 범위를 심하게 벗어남
표준화 후 데이터는 대략 N(0,1)인데 `padding_value=100`을 넣고 마스킹 없이
conv + AdaptiveAvgPool로 평균을 내므로, 패딩이 표현에 그대로 섞인다.
짧은 시퀀스일수록 왜곡이 크다. → 마스킹 또는 0 패딩 + 길이 정보 활용.

### A4. 스케일링이 매 epoch collate에서 반복됨
샘플 단위 표준화는 결정적 연산이므로 전처리 단계에서 1회 수행 가능.
(단, 실시간 추론에서도 같은 스케일링을 재현해야 하므로 변환 함수를 공용 모듈로 분리)

### A5. 검증/테스트 분리 없음
전체 데이터로 학습하고 학습 acc만 기록. 일반화 성능을 알 수 없다.
→ **종목 단위** train/val/test 분리 (같은 종목의 시퀀스가 양쪽에 들어가는 누수 방지),
시계열이므로 기간 기준 분리도 병행 검토.

### A6. 평가 지표 부재
3-클래스인데 `binary_accuracy` + 전체 acc만 기록. 클래스 2(무시)가 다수라 acc가 부풀려진다.
→ 클래스별 precision/recall/F1, confusion matrix. 실전 관점에서는
"고점/저점이라고 판정했을 때 실제로 맞는가"(precision)가 중요.

### A7. 저점/고점 처리 루프 중복
`create_dataset()`의 low/high 루프가 라벨 값만 다르고 동일. → 공통 함수로 통합.

### A8. ~~분봉 연도 추정 로직이 깨지기 쉬움~~ → 해소
`datetime.now().year` 기반 연도 복원은 실행 시점에 따라 결과가 달라진다.
**broker-modules SDK 도입으로 해소** — SDK가 완전한 timestamp를 제공하므로
연도 추정, 콤마 제거 등 HTS CSV 파싱 로직 자체가 사라진다. ([03_data_ingestion.md](03_data_ingestion.md) 참고)

### A9. requirements가 Windows/CUDA 고정
`torch==2.3.1+cu121`, `cupy-cuda12x` 등. 현재 개발 환경은 macOS.
→ 플랫폼 중립 의존성 정의 (pyproject.toml + uv), 시각화/실시간 등은 optional extra로 분리.
broker-modules가 Python 3.12+ / uv 기준이므로 이에 맞춘다.

### A10. ~~동률 극값이 하루 간격의 중복 샘플을 생성~~ → 해소
center rolling 창에서 동일한 최고가/최저가가 이어지면 legacy 방식은 각 봉을 모두 라벨해
시작점이 같고 끝점만 1~2봉 다른 샘플을 반복 생성한다. 신규 프리셋은
`fractal.tie_policy=plateau_last`를 기본으로 사용해 라벨 단계에서 같은 종류·같은 가격의
연속 plateau 후보 중 마지막 봉만 남긴다. 기존 schema v1 프리셋의 누락 필드는 과거 결과
재현을 위해 `all`로 해석한다. 라벨 정규화 후에도 남는 90% 이상 중첩 샘플은 제거하지 않고
Diagnostics의 overlap cluster/중복 추정 통계로 경고한다.

## B. 방법 개선 실험 — 재구현 후 하나씩 검증

### B1. 모델의 시간 축 수용 영역(receptive field) 확보
현재 CNN1D는 모든 conv가 `kernel_size=1`이라 시점 간 관계를 conv로는 전혀 못 본다
(pooling 평균으로만 섞임). 캔들 "패턴"을 학습하려는 의도와 모순.
→ kernel_size 3~5의 진짜 1D conv 스택, 또는 dilated conv / GRU / 소형 Transformer 비교 실험.
**재현 베이스라인(현 구조 그대로)을 먼저 만들고 비교할 것.**

### B2. 라벨 체계 재검토
클래스 2가 "역배열의 고점"과 "역배열의 저점"을 하나로 합친다.
→ (a) 현행 3클래스 유지, (b) 역배열 샘플 자체를 제외하고 2클래스,
(c) 4클래스(정배열 고/저 + 역배열 고/저), (d) "프랙탈 아님" 네거티브 샘플 추가 — 비교.
특히 (d): 현재는 프랙탈 지점만 샘플링하므로, 모델이 실전에서 매 봉마다 판정할 때
"프랙탈이 아닌 봉"을 본 적이 없다.

### B3. 스케일링 방식 비교
샘플 단위 StandardScaler vs 종가 대비 상대 변화율(수익률) vs min-max.
구 프로젝트에도 `kis_minmax_*` 체크포인트가 있는 걸 보면 이미 실험하던 주제.

### B4. 프랙탈 window 하이퍼파라미터 탐색
n=20은 실험값. 라벨 확정에 미래 `(n-1)//2`봉(n=20 → 9봉,
pandas center rolling 정렬 — docs/01 §3 참고)이 필요하므로
n이 커질수록 신호는 강해지지만 학습 가능한 샘플과 실시간성은 줄어든다.
(구 max_len 고정 길이 윈도우는 폐기 — 입력 윈도우는 직전 반대 마커 ~ 현재
마커의 스윙 구간으로 결정된다. docs/04 §1.2 참고)

### B5. 주석 처리된 필터들 재실험
정배열 필터, 유동성(거래대금) 필터 등을 라벨링 단계 옵션으로 정식화하고 효과 측정.

### B6. 학습 안정화
lr=0.01(Adam 기준 높음) 튜닝, 스케줄러, early stopping, 시드 고정.

### B7. Kronos 적응형 K-line 클리닝 효과 검증
[Kronos (Shi et al., arXiv:2508.02739) Appendix B](https://arxiv.org/abs/2508.02739)는
가격 필드 결측을 경계로 분할하고, 주기별 가격 점프·비유동·가격 정체 구간을 제거하는 전용
K-line 정제 절차를 제시한다. Pivot은 원천을 수정하지 않는 `kronos_adapted_v1` 정책으로
이를 적용한다. 기본 `report_only`와 `filter` 데이터셋을 동일 종목 split/seed로 비교해
클래스 분포, 유효 샘플 수, 클래스별 P/R/F1 변화를 측정한다. 국내 가격제한·분할·거래정지와
논문에 없는 틱봉 때문에 효과를 가정하지 않고 실험으로 판정한다. 논문의 Volume/Amount 5%
무작위 마스킹은 클리닝과 분리해 M4 train-only 증강 실험으로 둔다.

## C. 엔지니어링/구조

- 데이터 수집: HTS 수동 CSV → broker-modules SDK 조회로 대체, 수집(fetch)과 전처리를 분리하고
  조회 결과는 로컬 캐시(parquet)에 저장 — 상세 설계는 [03_data_ingestion.md](03_data_ingestion.md)
- 패키지 구조: `pivot/` 순수 도메인 패키지 + `server/`(FastAPI)·`web/`(React) 진입점 분리 —
  상세는 [05_package_layout.md](05_package_layout.md) (문서 → 구현 순서대로 추가)
- 실험 관리: 데이터셋/모델 메타를 json으로 남기던 방식은 유지하되 설정(config) 기반으로 정리
- 실시간 추론: 구 PyQt5 앱 대신 웹 Live 탭으로 재설계 ([04_webapp_design.md](04_webapp_design.md) §1.5,
  마일스톤 M5). 모델 성능 검증 뒤 구현하며, `infer_plot copy*.py` 난립의 원인이었던
  학습-추론 전처리 불일치는 `transforms` 공용 모듈로 차단
- 데이터 품질 진단: 원천 캐시/라벨/데이터셋이 학습 가능한 상태인지 별도 Diagnostics 탭에서 확인.
  누락·중복 timestamp, OHLC 이상값, MA NaN 비율, 라벨 분포, 90% 이상 overlap cluster,
  split 누수 여부를 학습 전 점검
- `.pkl` 직렬화(`tobytes`) 대신 parquet/npz 등 검토 — 가변 길이 시퀀스 저장 방식 결정 필요
