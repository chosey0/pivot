# 구 Fractal 프로젝트 파이프라인 정리

구 프로젝트 위치: `~/portfolio/Fractal`
이 문서는 재구현의 기준 명세다. 각 절에 원본 코드 위치를 병기한다.

## 0. 전체 흐름

```
원천 CSV (한국투자 HTS 다운로드)
   │  read_data()                 ── 파싱/정제/정렬
   ▼
캔들 DataFrame (OHLCV + MA 5/20/120)
   │  calc_fractal()              ── 윌리엄스 프랙탈 라벨 마킹
   ▼
fractal_high / fractal_low 컬럼 추가된 DataFrame
   │  create_dataset()            ── 프랙탈 지점별 시퀀스 샘플 생성
   ▼
학습 데이터셋 (.pkl + 메타 .json)
   │  collate() + DataLoader      ── 배치 단위 스케일링/패딩
   ▼
CNN1D 학습 (3-클래스 분류)
   ▼
평가 시각화(finplot) / 실시간 추론(PyQt5 + KIS 웹소켓)
```

## 1. 원천 데이터

> ⚠ 새 프로젝트에서는 이 절과 2절(read_data)의 CSV 기반 수집/파싱을
> broker-modules SDK 조회로 대체한다. → [03_data_ingestion.md](03_data_ingestion.md)

- 출처: 한국투자증권 HTS에서 종목별로 다운로드한 캔들 CSV
- 위치: `data/raw/한국투자/day/` (일봉), `data/raw/한국투자/min/` (분봉)
- 파일명 규칙: `{다운로드일자}_{종목명}.csv`, 분봉용 이평선 파일은 `*_ma.csv`
- 컬럼: `시간, 시가, 고가, 저가, 종가, 거래량, 거래대금, 5, 20, 120` (5/20/120 = 이동평균선)
- 행 순서: 최근 → 과거 (HTS 다운로드 형식 그대로)

## 2. 데이터 읽기/정제 — `read_data()`

원본: `scripts/data/dataset.py:10`

처리 순서:

1. CSV 읽기 (`encoding='utf-8'`, `engine='python'`)
2. 시간 파싱
   - **일봉**: `%Y-%m-%d` 그대로 파싱
   - **분봉**: 원본에 연도가 없어 현재 연도를 붙여 파싱. 데이터가 전년도로 이어지는 경우
     1월 마지막 행 인덱스를 기준으로 그 이후 행을 전년도로 되돌리는 방식 (3개 연도 이상은 미지원)
3. 천 단위 콤마 제거 (`replace(",", "")`), `dropna`
4. 타입 변환: OHLCV/거래대금 → `int64`, 이평선(5/20/120) → `float64`
5. 행 순서 뒤집기: 과거 → 최근 순으로 정렬
6. 컬럼명 영문화: `시간→Time, 시가→Open, 고가→High, 저가→Low, 종가→Close, 거래량→Volume, 거래대금→Amount`
7. (분봉 + `ma=True`일 때) 같은 파일명의 `*_ma.csv`(일봉 이평선)를 날짜 기준으로 merge
8. 인덱스: `Time`을 epoch milliseconds 정수로 변환해 설정 (finplot 시각화 호환 목적)
9. `callback` 인자로 받은 함수(관례상 `calc_fractal`)에 DataFrame을 넘겨 결과 반환

## 3. 프랙탈 라벨링 — `calc_fractal()`

원본: `scripts/data/fractal_indicator.py:4`

윌리엄스 프랙탈(Williams Fractal)의 일반화 버전:

- 크기 `n`(기본 실험값 **n=20**)의 **center rolling window**를 High/Low 시리즈에 적용
- 중심 봉의 High가 창 내 최댓값이면 → `fractal_high`에 해당 값 기록 (아니면 NaN)
- 중심 봉의 Low가 창 내 최솟값이면 → `fractal_low`에 해당 값 기록 (아니면 NaN)

**핵심 성질**: 프랙탈은 중심 봉 앞뒤로 `n//2`개의 봉을 봐야 확정되는 **후행 지표**다.
라벨은 미래 정보로 확정하되, 모델 입력은 해당 시점까지의 과거 데이터만 사용하므로
"이 봉이 나중에 프랙탈 고점/저점으로 확정될 것인가"를 예측하는 문제가 된다.

실험했다가 주석 처리된 필터 (재실험 후보):

- 정배열 필터: `20 > 120`이 아닌 구간의 프랙탈 제거
- 완전 정배열 필터: `5 > 20 > 120`이 아닌 구간 제거
- 유동성 필터: 거래대금 5억 미만 구간 제거

## 4. 샘플 생성 — `create_dataset()`

원본: `scripts/data/dataset.py:59`

- 사용 컬럼(`use_cols`): `Time, Open, High, Low, Close, 20, 120` (마지막 실험 기준. 5일선 미사용)
- `fractal_low`/`fractal_high`가 마킹된 각 지점에 대해:
  - 데이터 시작부터 해당 지점까지의 구간을 자르고, `max_len`(**20**)을 넘으면 뒤에서 20봉만 유지
  - 라벨 부여:
    - `0` = 저점 (fractal_low)
    - `1` = 고점 (fractal_high)
    - `2` = 무시 — 해당 시점에 `20일선 < 120일선`(역배열)이면 고점/저점 여부와 무관하게 2로 덮어씀
      (매매 신호로 쓸 만한 정배열 구간의 전환점만 학습 대상으로 삼으려는 의도)
- 저장 형식: 값들을 `int64`로 캐스팅 후 `ndarray.tobytes()`로 직렬화, `(data, label, shape, length)`
  컬럼의 DataFrame으로 묶어 `data/process/{dataset_name}.pkl`로 저장
- 메타 정보(`{dataset_name}.json`): 소스 경로, 샘플 수, 사용 컬럼, max_len, 클래스 비율

데이터셋 명명 규칙: `kis_day20_ma20120_cls3`
= 한국투자(kis) + 일봉/프랙탈 n=20(day20) + 이평 20/120 사용(ma20120) + 3클래스(cls3)

## 5. 배치 구성 — `collate()`

원본: `scripts/utils/collate.py:9`

- `tobytes` 버퍼를 `shape`대로 복원
- **샘플 단위로** `StandardScaler().fit_transform()` (샘플 내부 통계로 표준화 — 종목/가격대 불변성 확보 목적)
- 첫 컬럼(Time) 제거 → 피처 6개: `Open, High, Low, Close, 20, 120`
- `pad_sequence(batch_first=True, padding_value=100)`로 가변 길이 패딩
- 반환: `(padded_tensor, labels, lengths, [디버그용 DataFrame 리스트])`

## 6. 모델 — `CNN1D`

원본: `interface/models.py:4`

```
입력 (B, T, 6) → transpose → (B, 6, T)
Conv1d(6→12, k=1) → ReLU → AdaptiveAvgPool1d(8)
Conv1d(12→24, k=1) → ReLU → AdaptiveAvgPool1d(8)
Conv1d(24→48, k=1) → ReLU → AdaptiveAvgPool1d(8)
flatten → Linear(384→192) → ReLU → Linear(192→3)
```

- 모든 conv가 `kernel_size=1` (시점 간 패턴은 pooling 평균으로만 섞임 — 백로그 참고)
- 출력 3-클래스 로짓

## 7. 학습 — `train_main.py` + `scripts/models/train.py`

- 클래스 불균형: `label별 가중치 = 전체 수 / 클래스 수`로 `WeightedRandomSampler` 구성
- `DataLoader(batch_size=128, collate_fn=collate, sampler=...)` — 데이터셋은 shape 기준 정렬 후 로드
- `CrossEntropyLoss` + `Adam(lr=0.01)`, 스케줄러 없음(CosineAnnealing 실험 흔적), 10 epoch
- 5 epoch마다 체크포인트 저장: `models/saved/{dataset_name}_{epoch}_epoch.pth`
- epoch별 loss/acc/클래스 확률 히스토리를 `models/saved/{dataset_name}.json`으로 저장
- 검증 세트 분리 없음 (전체 데이터로 학습, 백로그 참고)

## 8. 평가/시각화 — `test_main.py`, `test_main_cls2.py`

- 특정 종목 CSV에 파이프라인 적용 → finplot 캔들차트 위에 실제 프랙탈 지점(점),
  이평선, 모델 예측을 겹쳐 그려 육안 평가
- 결과 이미지: `img/days/`, `img/mins/`

## 9. 실시간 추론 앱 — `main.py` + `core/` + `ui/`

- PyQt5 GUI: 종목 검색(`ui/search_bar.py`) + 구독 테이블(`ui/subscribe_table.py`)
- `core/agent.py`: 한국투자증권 OpenAPI 인증 (`env.yaml`에 키 보관, git 미추적)
- `core/websocket.py`: 실시간 체결 웹소켓 수신 → 구독 종목별 추론 스레드(`ui/infer_plot*.py`)로 전달
- 학습된 체크포인트(`kis_day20_ma20120_cls3_5_epoch.pth`)로 실시간 고점/저점 판정 시도
- `infer_plot copy*.py` 복사본 다수 — 실시간 차트+추론 부분을 실험하다 중단된 상태
- ebest(이베스트투자증권) API 연동 흔적도 있음: `core/api/ebest/`
