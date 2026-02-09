import { useState, useEffect, useRef, useCallback } from 'react'

export default function useWebSocket(url) {
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState([])
  const [insights, setInsights] = useState([])
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
        reconnectTimer.current = null
      }
    }

    ws.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data)
        if (data.type === 'event') {
          setEvents(prev => [data.event, ...prev].slice(0, 200))
        } else if (data.type === 'insight') {
          setInsights(prev => [data.output, ...prev].slice(0, 100))
          setEvents(prev => [data.output.event, ...prev].slice(0, 200))
        }
      } catch (e) {
        // ignore parse errors
      }
    }

    ws.onclose = () => {
      setConnected(false)
      reconnectTimer.current = setTimeout(connect, 3000)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [url])

  useEffect(() => {
    connect()
    return () => {
      if (wsRef.current) wsRef.current.close()
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
    }
  }, [connect])

  return { connected, events, insights }
}
