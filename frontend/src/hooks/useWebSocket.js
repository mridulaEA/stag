import { useState, useEffect, useRef, useCallback } from 'react'

export default function useWebSocket(url) {
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState([])
  const [insights, setInsights] = useState([])
  const [incidentUpdates, setIncidentUpdates] = useState([])
  const [lastError, setLastError] = useState(null)
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)
  const disposed = useRef(false)
  const backoff = useRef(1000)

  // Source-scoped key so the same event id from two sources is never collapsed.
  const keyOf = (e) => `${e.source}::${e.id}`
  const prepend = (list, item, max) => {
    const k = keyOf(item)
    return [item, ...list.filter(e => keyOf(e) !== k)].slice(0, max)
  }

  const connect = useCallback(() => {
    if (disposed.current) return
    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) return

    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      setLastError(null)
      backoff.current = 1000
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
        reconnectTimer.current = null
      }
    }

    ws.onmessage = (msg) => {
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
      } catch (e) {
        setLastError('malformed message from server')
      }
    }

    ws.onclose = () => {
      setConnected(false)
      if (disposed.current) return
      reconnectTimer.current = setTimeout(connect, backoff.current)
      backoff.current = Math.min(backoff.current * 2, 30000)
    }

    ws.onerror = () => {
      setLastError('connection error')
      ws.close()
    }
  }, [url])

  useEffect(() => {
    disposed.current = false
    connect()
    return () => {
      disposed.current = true
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [connect])

  return { connected, events, insights, incidentUpdates, lastError }
}
