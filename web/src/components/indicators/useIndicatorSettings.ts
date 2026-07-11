// 보조지표(MA/거래량) 표시·학습 피처 설정 상태 — 적용본과 편집 draft를 분리하고,
// 이름 붙인 프리셋을 브라우저 로컬 저장소에 보관한다 (M1 범위, docs/04 §1.1)
import { useEffect, useMemo, useState } from 'react'
import type { VisibleIndicators } from '../chart/CandleChart'

export type LineWidth = 1 | 2 | 3 | 4

export interface MovingAverageSetting {
  id: string
  window: number
  color: string
  lineWidth: LineWidth
  chart: boolean
  feature: boolean
}

export interface IndicatorPreset {
  name: string
  maSettings: MovingAverageSetting[]
  volumeChart: boolean
  volumeFeature: boolean
}

const INDICATOR_PRESET_STORAGE_KEY = 'pivot.indicatorPresets.v1'
export const DEFAULT_INDICATOR_PRESET_NAME = '기본 MA 5/20/60/120'

export const DEFAULT_MA_SETTINGS: MovingAverageSetting[] = [
  { id: 'ma-5', window: 5, color: '#009c62', lineWidth: 1, chart: true, feature: false },
  { id: 'ma-20', window: 20, color: '#e31b35', lineWidth: 1, chart: true, feature: true },
  { id: 'ma-60', window: 60, color: '#ff8a00', lineWidth: 1, chart: true, feature: false },
  { id: 'ma-120', window: 120, color: '#8a26b2', lineWidth: 1, chart: true, feature: true },
]

function cloneMaSettings(settings: MovingAverageSetting[]) {
  return settings.map((setting) => ({ ...setting }))
}

function defaultIndicatorPreset(): IndicatorPreset {
  return {
    name: DEFAULT_INDICATOR_PRESET_NAME,
    maSettings: cloneMaSettings(DEFAULT_MA_SETTINGS),
    volumeChart: true,
    volumeFeature: false,
  }
}

function loadIndicatorPresets(): IndicatorPreset[] {
  if (typeof window === 'undefined') return [defaultIndicatorPreset()]
  const fallback = [defaultIndicatorPreset()]
  const raw = window.localStorage.getItem(INDICATOR_PRESET_STORAGE_KEY)
  if (!raw) return fallback
  try {
    const parsed = JSON.parse(raw) as IndicatorPreset[]
    return parsed.length > 0 ? parsed : fallback
  } catch {
    return fallback
  }
}

function saveIndicatorPresets(presets: IndicatorPreset[]) {
  window.localStorage.setItem(INDICATOR_PRESET_STORAGE_KEY, JSON.stringify(presets))
}

export function featureColumnsFor(settings: MovingAverageSetting[], includeVolume: boolean) {
  return [
    'Open',
    'High',
    'Low',
    'Close',
    ...(includeVolume ? ['Volume'] : []),
    ...settings.filter((setting) => setting.feature).map((setting) => String(setting.window)),
  ]
}

export function duplicateWindows(settings: MovingAverageSetting[]) {
  const seen = new Set<number>()
  const duplicated = new Set<number>()
  for (const setting of settings) {
    if (seen.has(setting.window)) duplicated.add(setting.window)
    seen.add(setting.window)
  }
  return [...duplicated].sort((a, b) => a - b)
}

export function useIndicatorSettings({ onMessage }: { onMessage: (text: string) => void }) {
  const [indicatorPanelOpen, setIndicatorPanelOpen] = useState(false)
  const [maSettings, setMaSettings] = useState<MovingAverageSetting[]>(DEFAULT_MA_SETTINGS)
  const [volumeChart, setVolumeChart] = useState(true)
  const [volumeFeature, setVolumeFeature] = useState(false)
  const [draftMaSettings, setDraftMaSettings] = useState<MovingAverageSetting[]>(DEFAULT_MA_SETTINGS)
  const [draftVolumeChart, setDraftVolumeChart] = useState(true)
  const [draftVolumeFeature, setDraftVolumeFeature] = useState(false)
  const [indicatorPresets, setIndicatorPresets] = useState<IndicatorPreset[]>(loadIndicatorPresets)
  const [selectedIndicatorPreset, setSelectedIndicatorPreset] = useState(DEFAULT_INDICATOR_PRESET_NAME)
  const [presetNameInput, setPresetNameInput] = useState(DEFAULT_INDICATOR_PRESET_NAME)

  const visibleIndicators = useMemo<VisibleIndicators>(
    () => ({
      movingAverages: maSettings
        .filter((setting) => setting.chart)
        .map((setting) => ({
          window: String(setting.window),
          color: setting.color,
          lineWidth: setting.lineWidth,
        })),
      volume: volumeChart,
    }),
    [maSettings, volumeChart],
  )
  const maWindows = useMemo(
    () => Array.from(new Set(maSettings.map((setting) => setting.window))).sort((a, b) => a - b),
    [maSettings],
  )
  const legendText = useMemo(
    () =>
      maSettings
        .filter((setting) => setting.chart)
        .map((setting) => setting.window)
        .join(' '),
    [maSettings],
  )

  useEffect(() => {
    saveIndicatorPresets(indicatorPresets)
  }, [indicatorPresets])

  function openIndicatorPanel() {
    setDraftMaSettings(cloneMaSettings(maSettings))
    setDraftVolumeChart(volumeChart)
    setDraftVolumeFeature(volumeFeature)
    setPresetNameInput(selectedIndicatorPreset)
    setIndicatorPanelOpen(true)
  }

  function applyIndicatorDraft() {
    setMaSettings(cloneMaSettings(draftMaSettings))
    setVolumeChart(draftVolumeChart)
    setVolumeFeature(draftVolumeFeature)
    setIndicatorPanelOpen(false)
  }

  function cancelIndicatorDraft() {
    setDraftMaSettings(cloneMaSettings(maSettings))
    setDraftVolumeChart(volumeChart)
    setDraftVolumeFeature(volumeFeature)
    setIndicatorPanelOpen(false)
  }

  function resetIndicatorDraft() {
    setDraftMaSettings(cloneMaSettings(DEFAULT_MA_SETTINGS))
    setDraftVolumeChart(true)
    setDraftVolumeFeature(false)
    setPresetNameInput(DEFAULT_INDICATOR_PRESET_NAME)
  }

  function updateDraftMaSetting(id: string, patch: Partial<Omit<MovingAverageSetting, 'id'>>) {
    setDraftMaSettings((current) =>
      current.map((setting) => (setting.id === id ? { ...setting, ...patch } : setting)),
    )
  }

  function removeDraftMaSetting(id: string) {
    setDraftMaSettings((current) => current.filter((setting) => setting.id !== id))
  }

  function addDraftMaSetting() {
    setDraftMaSettings((current) => {
      const used = new Set(current.map((setting) => setting.window))
      const nextWindow = [5, 10, 20, 60, 120, 240].find((window) => !used.has(window)) ?? 20
      return [
        ...current,
        {
          id: `ma-${Date.now()}`,
          window: nextWindow,
          color: '#60a5fa',
          lineWidth: 1,
          chart: true,
          feature: false,
        },
      ]
    })
  }

  function applyIndicatorPreset(name: string) {
    const preset = indicatorPresets.find((item) => item.name === name)
    if (!preset) return
    setSelectedIndicatorPreset(name)
    setPresetNameInput(name)
    setDraftMaSettings(cloneMaSettings(preset.maSettings))
    setDraftVolumeChart(preset.volumeChart)
    setDraftVolumeFeature(preset.volumeFeature)
  }

  function saveCurrentIndicatorPreset() {
    const name = presetNameInput.trim()
    if (!name) return
    const nextPreset: IndicatorPreset = {
      name,
      maSettings: cloneMaSettings(draftMaSettings),
      volumeChart: draftVolumeChart,
      volumeFeature: draftVolumeFeature,
    }
    setIndicatorPresets((current) => {
      const withoutSameName = current.filter((preset) => preset.name !== name)
      return [...withoutSameName, nextPreset]
    })
    setSelectedIndicatorPreset(name)
    onMessage(`보조지표 프리셋 '${name}'을 저장했습니다.`)
  }

  function deleteSelectedIndicatorPreset() {
    if (selectedIndicatorPreset === DEFAULT_INDICATOR_PRESET_NAME) return
    setIndicatorPresets((current) =>
      current.filter((preset) => preset.name !== selectedIndicatorPreset),
    )
    applyIndicatorPreset(DEFAULT_INDICATOR_PRESET_NAME)
  }

  return {
    indicatorPanelOpen,
    openIndicatorPanel,
    maSettings,
    setMaSettings,
    volumeChart,
    setVolumeChart,
    volumeFeature,
    visibleIndicators,
    maWindows,
    legendText,
    draftMaSettings,
    draftVolumeChart,
    setDraftVolumeChart,
    draftVolumeFeature,
    setDraftVolumeFeature,
    indicatorPresets,
    selectedIndicatorPreset,
    presetNameInput,
    setPresetNameInput,
    applyIndicatorDraft,
    cancelIndicatorDraft,
    resetIndicatorDraft,
    updateDraftMaSetting,
    removeDraftMaSetting,
    addDraftMaSetting,
    applyIndicatorPreset,
    saveCurrentIndicatorPreset,
    deleteSelectedIndicatorPreset,
  }
}

export type IndicatorSettings = ReturnType<typeof useIndicatorSettings>
