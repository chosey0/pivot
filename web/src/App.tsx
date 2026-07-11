import { useState } from 'react'
import { Datasets } from './pages/Datasets'
import { Diagnostics } from './pages/Diagnostics'
import { Lab } from './pages/Lab'
import { Watchlist } from './pages/Watchlist'
import './App.css'

type TabId = 'watchlist' | 'lab' | 'datasets' | 'diagnostics' | 'training' | 'live'

const TABS: { id: TabId; label: string }[] = [
  { id: 'watchlist', label: '종목 & 데이터' },
  { id: 'lab', label: '전처리 실험실' },
  { id: 'datasets', label: '데이터셋' },
  { id: 'diagnostics', label: '데이터 진단' },
  { id: 'training', label: '학습' },
  { id: 'live', label: '실시간' },
]

function renderPlaceholder(title: string) {
  return (
    <section className="placeholder">
      <h2>{title}</h2>
      <p>M1 범위에서는 화면 자리만 잡아 둡니다. 이후 마일스톤에서 실제 기능을 연결합니다.</p>
    </section>
  )
}

function App() {
  const [activeTab, setActiveTab] = useState<TabId>('watchlist')
  const [subtitle, setSubtitle] = useState<string | null>(null)

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>pivot</h1>
          <span className="app-subtitle">{subtitle ?? 'M1 data ingestion'}</span>
        </div>
        <nav className="tabs" aria-label="주요 화면">
          {TABS.map((tab) => (
            <button
              className={tab.id === activeTab ? 'tab active' : 'tab'}
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              type="button"
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="app-main">
        {/* 탭 전환에도 차트/선택 상태가 유지되도록 항상 마운트하고 표시만 제어한다 */}
        <Watchlist active={activeTab === 'watchlist'} onSubtitleChange={setSubtitle} />
        {activeTab === 'lab' && <Lab />}
        {activeTab === 'datasets' && <Datasets />}
        {activeTab === 'diagnostics' && <Diagnostics />}
        {activeTab === 'training' && renderPlaceholder('학습: M4에서 run 관리와 평가 지표를 연결합니다.')}
        {activeTab === 'live' && renderPlaceholder('실시간: M5에서 WebSocket 추론을 연결합니다.')}
      </main>
    </div>
  )
}

export default App
