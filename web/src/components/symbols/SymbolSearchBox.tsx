import { useEffect, useState, type KeyboardEvent } from 'react'
import { api, type InstrumentRegion, type SymbolSuggestion } from '../../api/client'

interface SymbolSearchBoxProps {
  region: InstrumentRegion
  query: string
  selectedSymbol: string
  placeholder: string
  disabled?: boolean
  excludeSymbols?: ReadonlySet<string>
  onQueryChange: (query: string) => void
  onSelect: (item: SymbolSuggestion) => void
  onError: (message: string) => void
}

export function SymbolSearchBox({
  region,
  query,
  selectedSymbol,
  placeholder,
  disabled = false,
  excludeSymbols,
  onQueryChange,
  onSelect,
  onError,
}: SymbolSearchBoxProps) {
  const [suggestions, setSuggestions] = useState<SymbolSuggestion[]>([])
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    const normalized = query.trim()
    if (normalized.length < 2 || selectedSymbol) {
      setSuggestions([])
      setOpen(false)
      setLoading(false)
      return
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      setLoading(true)
      api
        .symbolSearch(normalized, region, controller.signal)
        .then((rows) => {
          const available = excludeSymbols
            ? rows.filter((row) => !excludeSymbols.has(row.symbol))
            : rows
          setSuggestions(available)
          setActiveIndex(0)
          setOpen(available.length > 0)
        })
        .catch((error: Error) => {
          if (error.name !== 'AbortError') onError(error.message)
        })
        .finally(() => setLoading(false))
    }, 180)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [excludeSymbols, onError, query, region, selectedSymbol])

  function select(item: SymbolSuggestion) {
    onSelect(item)
    setSuggestions([])
    setOpen(false)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (!open || suggestions.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((current) => (current + 1) % suggestions.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((current) => (current === 0 ? suggestions.length - 1 : current - 1))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      select(suggestions[activeIndex])
    } else if (event.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div className="symbol-search-box">
      <input
        autoComplete="off"
        disabled={disabled}
        onChange={(event) => onQueryChange(event.target.value)}
        onFocus={() => setOpen(suggestions.length > 0)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        value={query}
      />
      {(open || loading) && (
        <div className="symbol-suggestions" role="listbox">
          {loading && suggestions.length === 0 ? (
            <div className="symbol-suggestion muted">검색 중...</div>
          ) : (
            suggestions.map((item, index) => (
              <button
                className={index === activeIndex ? 'symbol-suggestion active' : 'symbol-suggestion'}
                key={`${item.market}:${item.symbol}`}
                onMouseDown={(event) => {
                  event.preventDefault()
                  select(item)
                }}
                onMouseEnter={() => setActiveIndex(index)}
                role="option"
                type="button"
              >
                <strong>{item.name}</strong>
                <span>{item.symbol}</span>
                <em>{item.market}</em>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}
