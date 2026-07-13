/** 이벤트 시각 표시 — 일봉 'yyyy-mm-dd' 문자열은 그대로, 분/틱 unix 초는 KST 벽시계 복원. */
export function formatEventTime(value: string | number) {
  if (typeof value === 'number') {
    // 백엔드가 naive(KST 벽시계) 시각을 UTC epoch로 직렬화하므로 UTC getter로 복원
    const date = new Date(value * 1000)
    const pad = (part: number) => String(part).padStart(2, '0')
    return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`
  }
  return String(value)
}
