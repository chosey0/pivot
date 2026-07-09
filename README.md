# Pivot

윌리엄스 프랙탈 지표로 캔들스틱 데이터에 고점/저점 라벨을 자동 생성하고,
해당 시점까지의 시퀀스를 모델에 입력해 **시퀀스 마지막 시점이 프랙탈 고점/저점으로 확정될지**를
분류(사실상 예측)하는 프로젝트.

구 [`Fractal`](../Fractal) 프로젝트의 후속. 코드를 그대로 옮기지 않고,
기존 파이프라인을 문서로 정리한 뒤 개선하면서 재구현한다.
**데이터 전처리 방법(프랙탈 라벨링)은 기존과 동일하게 유지**하되,
원천 데이터는 HTS 수동 CSV 대신 [broker-modules](https://github.com/chosey0/broker-modules)
증권사 OpenAPI SDK로 직접 조회한다.

## 진행 방식

1. **문서화** — 구 프로젝트의 파이프라인/설계를 문서로 정리 ← 현재 단계
2. **재구현** — 문서 기반으로 전처리부터 순서대로 새로 구현
3. **개선** — 백로그의 개선 항목을 실험하며 반영

## 문서

| 문서 | 내용 |
|---|---|
| [docs/01_legacy_pipeline.md](docs/01_legacy_pipeline.md) | 구 Fractal 프로젝트 파이프라인 정리 (데이터 → 라벨링 → 학습 → 실시간 추론) |
| [docs/02_improvement_backlog.md](docs/02_improvement_backlog.md) | 구 코드에서 발견된 문제점과 개선 백로그 |
| [docs/03_data_ingestion.md](docs/03_data_ingestion.md) | 데이터 수집 설계 (broker-modules SDK 사용) |
