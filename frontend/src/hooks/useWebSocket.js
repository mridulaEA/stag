import { useState, useEffect, useRef, useCallback } from 'react'

// Source-scoped key so the same event id from two sources is never collapsed (matches StreamEvent.doc_key).
const keyOf = (e) => JSON.stringify([e.source, e.id])
const prepend = (list, item, max) => {
  const k = keyOf(item)
  const prev = list.find(e => keyOf(e) === k)
  // keep trigger / suppression metadata when a later message carries the same event
  const merged = prev ? { ...prev, ...item, _trigger: item._trigger ?? prev._trigger, _suppressed: item._suppressed ?? prev._suppressed } : item
  return [merged, ...list.filter(e => keyOf(e) !== k)].slice(0, max)
}

export default function useWebSocket(url) {
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState([])
  const [insights, setInsights] = useState([])
  const [incidentUpdates, setIncidentUpdates] = useState([])
  const [lastError, setLastError] = useState(null)
  const wsRef = useRef(null)

  const hydrate = useCallback(async () => {
    try {
      const [ev, ins] = await Promise.all([fetch('/api/events?limit=200'), fetch('/api/insights?limit=100')])
      if (ev.ok) {
        const data = await ev.json()
        setEvents(prev => (data.events || []).slice().reverse().reduce((acc, e) => prepend(acc, e, 200), prev))
      }
      if (ins.ok) {
        const data = await ins.json()
        setInsights(prev => {
          const seen = new Set(prev.map(o => o.insight?.id))
          const fresh = (data.insights || []).filter(o => !seen.has(o.insight?.id)).reverse()
          return [...fresh, ...prev].slice(0, 100)
        })
      }
    } catch {
      /* hydration is best-effort */
    }
  }, [])

  useEffect(() => {
    // Every effect generation owns its own socket, timer and disposed flag. Handlers from an
    // earlier generation (StrictMode double-invoke, URL change, late close) are inert because
    // they are bound to a `gen` object that is no longer current.
    const gen = { disposed: false, timer: null, backoff: 1000, ws: null }

    const connect = () => {
      if (gen.disposed) return
      const ws = new WebSocket(url)
      gen.ws = ws
      wsRef.current = ws
      const isCurrent = () => !gen.disposed && gen.ws === ws

      ws.onopen = () => {
        if (!isCurrent()) return
        setConnected(true)
        setLastError(null)
        gen.backoff = 1000
        hydrate()
      }
      ws.onmessage = (msg) => {
        if (!isCurrent()) return
        try {
          const data = JSON.parse(msg.data)
          if (data.type === 'event') {
            setEvents(prev => prepend(prev, data.event, 200))
          } else if (data.type === 'trigger') {
            setEvents(prev => prepend(prev, { ...data.event, _trigger: data.trigger?.rule_name }, 200))
          } else if (data.type === 'insight') {
            setInsights(prev => [data.output, ...prev].slice(0, 100))
            setEvents(prev => prepend(prev, data.output.event, 200))
          } else if (data.type === 'incident_update') {
            setIncidentUpdates(prev => [data, ...prev].slice(0, 100))
            if (data.event) setEvents(prev => prepend(prev, { ...data.event, _suppressed: data.decision?.reason }, 200))
          }
        } catch {
          setLastError('malformed message from server')
        }
      }
      ws.onclose = () => {
        if (!isCurrent()) return
        setConnected(false)
        gen.timer = setTimeout(connect, gen.backoff)
        gen.backoff = Math.min(gen.backoff * 2, 30000)
      }
      ws.onerror = () => {
        if (!isCurrent()) return
        setLastError('connection error')
        ws.close()
      }
    }

    connect()
    return () => {
      gen.disposed = true
      if (gen.timer) clearTimeout(gen.timer)
      const ws = gen.ws
      gen.ws = null // detach handlers before closing so the close event is inert
      if (ws) {
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
        try { ws.close() } catch { /* already closed */ }
      }
      if (wsRef.current === ws) wsRef.current = null
    }
  }, [url, hydrate])

  return { connected, events, insights, incidentUpdates, lastError }
}
