"""Supabase 학습 데이터 저장소 경계 (docs/06).

프리셋 이후 파생 데이터의 단일 원본. 메타데이터는 PostgREST, 대용량
바이너리는 private Storage로 접근한다. 서버 전용 secret 키로만 동작하며
브라우저에는 어떤 키/객체 URL도 전달하지 않는다.
"""
