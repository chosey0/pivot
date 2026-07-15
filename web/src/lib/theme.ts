import { useSyncExternalStore } from 'react'

export type ThemeMode = 'light' | 'dark'

const STORAGE_KEY = 'pivot.theme'
const listeners = new Set<() => void>()

function stored(): ThemeMode | null {
  const value = localStorage.getItem(STORAGE_KEY)
  return value === 'light' || value === 'dark' ? value : null
}

/** 저장된 선택이 없으면 시스템 설정을 초기값으로 쓴다. */
function initial(): ThemeMode {
  return stored() ?? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
}

// 첫 페인트 전에 <html data-theme>를 세워 밝은 화면이 번쩍이지 않게 한다 (main.tsx가 이 모듈을 먼저 import)
let current: ThemeMode = initial()
document.documentElement.dataset.theme = current

export function setTheme(next: ThemeMode) {
  if (next === current) return
  current = next
  document.documentElement.dataset.theme = next
  localStorage.setItem(STORAGE_KEY, next)
  for (const listener of listeners) listener()
}

export function toggleTheme() {
  setTheme(current === 'dark' ? 'light' : 'dark')
}

export function useTheme(): ThemeMode {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => current,
  )
}

/**
 * 차트 라이브러리는 CSS를 모르므로 색을 값으로 넘겨야 한다.
 * 팔레트가 두 벌로 갈라지지 않도록 index.css의 토큰을 그대로 읽는다.
 */
export function chartTheme() {
  const style = getComputedStyle(document.documentElement)
  return {
    grid: style.getPropertyValue('--chart-grid').trim(),
    axisText: style.getPropertyValue('--chart-axis-text').trim(),
    text: style.getPropertyValue('--text').trim(),
    muted: style.getPropertyValue('--muted').trim(),
  }
}
