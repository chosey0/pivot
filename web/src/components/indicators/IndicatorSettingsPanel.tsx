// + 보조지표 설정 모달 — draft를 편집하고 적용/취소/초기화로 반영을 통제한다.
// 상태는 useIndicatorSettings가 소유하고 여기서는 렌더링만 한다.
import { useMemo } from 'react'
import {
  DEFAULT_INDICATOR_PRESET_NAME,
  duplicateWindows,
  featureColumnsFor,
  type IndicatorSettings,
  type LineWidth,
} from './useIndicatorSettings'

interface IndicatorSettingsPanelProps {
  settings: IndicatorSettings
  barCount: number
}

export function IndicatorSettingsPanel({ settings, barCount }: IndicatorSettingsPanelProps) {
  const {
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
  } = settings

  const draftFeatureColumns = useMemo(
    () => featureColumnsFor(draftMaSettings, draftVolumeFeature),
    [draftMaSettings, draftVolumeFeature],
  )
  const draftDuplicateWindows = useMemo(() => duplicateWindows(draftMaSettings), [draftMaSettings])
  const draftFeatureDimension = draftFeatureColumns.length
  const chartOnlyIndicators = useMemo(
    () => [
      ...draftMaSettings
        .filter((setting) => setting.chart && !setting.feature)
        .map((setting) => `MA${setting.window}`),
      ...(draftVolumeChart && !draftVolumeFeature ? ['Volume'] : []),
    ],
    [draftMaSettings, draftVolumeChart, draftVolumeFeature],
  )
  const featureOnlyIndicators = useMemo(
    () => [
      ...draftMaSettings
        .filter((setting) => setting.feature && !setting.chart)
        .map((setting) => `MA${setting.window}`),
      ...(draftVolumeFeature && !draftVolumeChart ? ['Volume'] : []),
    ],
    [draftMaSettings, draftVolumeChart, draftVolumeFeature],
  )
  const nanRiskIndicators = useMemo(() => {
    return draftMaSettings
      .filter((setting) => setting.feature)
      .map((setting) => ({
        label: `MA${setting.window}`,
        missingBars: Math.max(setting.window - 1, 0),
        tooLong: barCount > 0 && setting.window > barCount,
      }))
      .filter((item) => item.missingBars > 0)
  }, [barCount, draftMaSettings])

  return (
    <div className="indicator-overlay" role="dialog" aria-modal="true">
      <div className="indicator-modal">
      <aside className="indicator-menu">
        <h3>상단 지표</h3>
        <button className="indicator-menu-item active" type="button">
          <span>이동평균선</span>
          <span className="check-dot">✓</span>
        </button>
        {['일목균형표', '볼린저 밴드', '슈퍼트렌드', '매물대분석', '엔벨로프', '윌리엄스 프랙탈'].map(
          (item) => (
            <button className="indicator-menu-item muted" disabled key={item} type="button">
              <span>{item}</span>
              <span>⌄</span>
            </button>
          ),
        )}
      </aside>
      <section className="indicator-editor">
        <div className="indicator-editor-head">
          <div>
            <h3>이동평균선</h3>
            <p>지난 n일 동안 주가 평균값을 이은 선</p>
          </div>
          <button
            className="modal-close"
            onClick={cancelIndicatorDraft}
            type="button"
            aria-label="보조지표 닫기"
          >
            ×
          </button>
        </div>

        <div className="preset-bar">
          <label>
            프리셋
            <select
              onChange={(event) => applyIndicatorPreset(event.target.value)}
              value={selectedIndicatorPreset}
            >
              {indicatorPresets.map((preset) => (
                <option key={preset.name} value={preset.name}>
                  {preset.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            이름
            <input
              onChange={(event) => setPresetNameInput(event.target.value)}
              placeholder="프리셋 이름"
              value={presetNameInput}
            />
          </label>
          <button className="secondary-action" onClick={saveCurrentIndicatorPreset} type="button">
            저장
          </button>
          <button
            className="secondary-action"
            disabled={selectedIndicatorPreset === DEFAULT_INDICATOR_PRESET_NAME}
            onClick={deleteSelectedIndicatorPreset}
            type="button"
          >
            삭제
          </button>
        </div>

        <div className="ma-settings">
          {draftMaSettings.map((setting, index) => (
            <div className="ma-setting-row" key={setting.id}>
              <span className="period-label">기간{index + 1}</span>
              <label className="swatch-field">
                <input
                  aria-label={`기간${index + 1} 색상`}
                  onChange={(event) =>
                    updateDraftMaSetting(setting.id, { color: event.target.value })
                  }
                  type="color"
                  value={setting.color}
                />
              </label>
              <select
                aria-label={`기간${index + 1} 선 굵기`}
                onChange={(event) =>
                  updateDraftMaSetting(setting.id, {
                    lineWidth: Number(event.target.value) as LineWidth,
                  })
                }
                value={setting.lineWidth}
              >
                {[1, 2, 3, 4].map((width) => (
                  <option key={width} value={width}>
                    {width}px
                  </option>
                ))}
              </select>
              <select aria-label={`기간${index + 1} 기준값`} disabled value="Close">
                <option value="Close">종가</option>
              </select>
              <input
                aria-label={`기간${index + 1} 값`}
                min={1}
                onChange={(event) =>
                  updateDraftMaSetting(setting.id, {
                    window: Math.max(1, Number(event.target.value) || 1),
                  })
                }
                type="number"
                value={setting.window}
              />
              <label className="compact-check">
                <input
                  checked={setting.chart}
                  onChange={() => updateDraftMaSetting(setting.id, { chart: !setting.chart })}
                  type="checkbox"
                />
                차트
              </label>
              <label className="compact-check">
                <input
                  checked={setting.feature}
                  onChange={() => updateDraftMaSetting(setting.id, { feature: !setting.feature })}
                  type="checkbox"
                />
                학습
              </label>
              <button
                className="icon-button"
                disabled={draftMaSettings.length <= 1}
                onClick={() => removeDraftMaSetting(setting.id)}
                type="button"
                aria-label={`기간${index + 1} 삭제`}
              >
                ×
              </button>
            </div>
          ))}
        </div>

        <div className="indicator-actions">
          <button className="add-period" onClick={addDraftMaSetting} type="button">
            <span>＋</span>
            기간 추가
          </button>
          <label className="compact-check volume-toggle">
            <input
              checked={draftVolumeChart}
              onChange={() => setDraftVolumeChart((current) => !current)}
              type="checkbox"
            />
            거래량 표시
          </label>
          <label className="compact-check volume-toggle">
            <input
              checked={draftVolumeFeature}
              onChange={() => setDraftVolumeFeature((current) => !current)}
              type="checkbox"
            />
            거래량 학습 피처
          </label>
        </div>

        <div className="indicator-diagnostics">
          <div className="feature-preview">
            <strong>전처리 프리셋 features</strong>
            <span>{draftFeatureColumns.join(', ')}</span>
            <em>입력 차원 {draftFeatureDimension}</em>
          </div>
          {draftDuplicateWindows.length > 0 && (
            <p className="warning">중복 기간: {draftDuplicateWindows.join(', ')}. 같은 MA 컬럼은 하나로 병합하는 편이 안전합니다.</p>
          )}
          {chartOnlyIndicators.length > 0 && (
            <p className="notice">차트에만 표시: {chartOnlyIndicators.join(', ')}</p>
          )}
          {featureOnlyIndicators.length > 0 && (
            <p className="notice">학습에만 포함: {featureOnlyIndicators.join(', ')}</p>
          )}
          {nanRiskIndicators.length > 0 && (
            <p className="notice">
              NaN 주의: {nanRiskIndicators.map((item) => `${item.label} 앞 ${item.missingBars}봉`).join(', ')}
              {nanRiskIndicators.some((item) => item.tooLong) ? ' · 현재 봉 수보다 긴 기간이 있습니다.' : ''}
            </p>
          )}
        </div>

        <div className="modal-actions">
          <button className="secondary-action" onClick={resetIndicatorDraft} type="button">
            초기화
          </button>
          <button className="secondary-action" onClick={cancelIndicatorDraft} type="button">
            취소
          </button>
          <button className="apply-action" onClick={applyIndicatorDraft} type="button">
            적용
          </button>
        </div>
      </section>
      </div>
    </div>
  )
}
