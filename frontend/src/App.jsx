import React, { useState, useEffect } from 'react'
import {
  Activity, Zap, Database, Brain, Radio, AlertTriangle,
  Shield, ArrowRight, ChevronDown, ChevronUp, Search,
  Circle, Layers, GitBranch, BarChart3, Users, Play, UserCheck
} from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts'
import useWebSocket from './hooks/useWebSocket'

const SEVERITY_COLORS = {
  critical: '#ef4444',
  high: '#f97316',
  medium: '#eab308',
  low: '#22c55e',
}

const SEVERITY_BG = {
  critical: 'bg-red-500/20 text-red-400 border-red-500/30',
  high: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  medium: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  low: 'bg-green-500/20 text-green-400 border-green-500/30',
}

function SeverityBadge({ severity }) {
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium border ${SEVERITY_BG[severity] || SEVERITY_BG.low}`}>
      {severity}
    </span>
  )
}

function StatCard({ icon: Icon, label, value, sub, color = 'text-stag-400' }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-center gap-2 mb-2">
        <Icon size={16} className={color} />
        <span className="text-xs text-gray-400 uppercase tracking-wider">{label}</span>
      </div>
      <div className="text-2xl font-bold">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  )
}

function InsightCard({ insight, defaultExpanded = false }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const output = insight
  const event = output.event
  const trigger = output.trigger
  const generated = output.insight

  return (
    <div className={`bg-gray-900 border rounded-lg p-4 animate-slide-in ${
      generated.severity === 'critical' ? 'border-red-500/40' :
      generated.severity === 'high' ? 'border-orange-500/40' :
      'border-gray-800'
    }`}>
      <div className="flex items-start justify-between cursor-pointer" onClick={() => setExpanded(!expanded)}>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <SeverityBadge severity={generated.severity} />
            <span className="text-xs text-gray-500">{trigger.rule_name}</span>
            <span className="text-xs text-gray-600">{new Date(event.timestamp).toLocaleTimeString()}</span>
          </div>
          <h3 className="font-medium text-sm truncate">{generated.title}</h3>
          <p className="text-xs text-gray-400 mt-1 line-clamp-2">{generated.summary}</p>
        </div>
        <div className="ml-2 text-gray-500">
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </div>
      </div>

      {expanded && (
        <div className="mt-3 pt-3 border-t border-gray-800 space-y-3">
          <div>
            <h4 className="text-xs text-gray-500 uppercase mb-1">Analysis</h4>
            <p className="text-sm text-gray-300">{generated.detail}</p>
          </div>
          <div>
            <h4 className="text-xs text-gray-500 uppercase mb-1">Recommendations</h4>
            <ul className="space-y-1">
              {generated.recommendations?.map((rec, i) => (
                <li key={i} className="text-sm text-gray-400 flex items-start gap-2">
                  <ArrowRight size={12} className="text-stag-500 mt-1 shrink-0" />
                  {rec}
                </li>
              ))}
            </ul>
          </div>
          <div className="flex items-center gap-4 text-xs text-gray-600">
            <span>Confidence: {(generated.confidence * 100).toFixed(0)}%</span>
            <span>Pipeline: {output.pipeline_time_ms?.toFixed(0)}ms</span>
            <span>Sources: {output.retrieval?.fused_results?.length || 0}</span>
          </div>
        </div>
      )}
    </div>
  )
}

function EventRow({ event }) {
  const payload = event.payload || {}
  return (
    <div className="flex items-center gap-3 py-2 px-3 hover:bg-gray-800/50 rounded text-sm">
      <div className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: SEVERITY_COLORS[event.severity] }} />
      <span className="text-gray-500 text-xs w-16 shrink-0">{new Date(event.timestamp).toLocaleTimeString()}</span>
      <SeverityBadge severity={event.severity} />
      <span className="text-gray-300 truncate flex-1">{payload.description || payload.message || event.source}</span>
      {event._trigger && <span className="text-xs text-yellow-500">⚡ {event._trigger}</span>}
      {event._suppressed && <span className="text-xs text-gray-500" title={event._suppressed}>deduped</span>}
      <span className="text-xs text-gray-600">{event.event_type}</span>
    </div>
  )
}

function PipelineFlow({ stats }) {
  const steps = [
    { icon: Radio, label: 'Stream', value: stats?.events_processed || 0, color: 'text-blue-400' },
    { icon: Zap, label: 'Trigger', value: stats?.triggers_fired || 0, color: 'text-yellow-400' },
    { icon: Search, label: 'Retrieve', value: stats?.retriever?.vector_count || 0, color: 'text-purple-400' },
    { icon: Brain, label: 'Generate', value: stats?.insights_generated || 0, color: 'text-stag-400' },
  ]
  return (
    <div className="flex items-center gap-1">
      {steps.map((step, i) => (
        <React.Fragment key={step.label}>
          <div className="flex items-center gap-2 bg-gray-900 border border-gray-800 rounded-lg px-3 py-2">
            <step.icon size={14} className={step.color} />
            <div>
              <div className="text-xs text-gray-500">{step.label}</div>
              <div className="text-sm font-mono font-bold">{step.value}</div>
            </div>
          </div>
          {i < steps.length - 1 && <ArrowRight size={14} className="text-gray-700 shrink-0" />}
        </React.Fragment>
      ))}
    </div>
  )
}

const STEP_COLORS = {
  coordinate: 'text-blue-400', transition: 'text-gray-400', investigate: 'text-purple-400',
  propose_action: 'text-yellow-400', authorize: 'text-orange-400', dispatch: 'text-red-400',
  verify: 'text-green-400', notify: 'text-cyan-400', record: 'text-gray-300',
}

function CrewCard({ crew }) {
  const [expanded, setExpanded] = useState(false)
  const [run, setRun] = useState(null)
  const [running, setRunning] = useState(false)

  const runCrew = async (autoApprove) => {
    setRunning(true)
    try {
      const res = await fetch(`/api/crews/${crew.name}/run`, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ auto_approve: autoApprove }),
      })
      if (res.ok) setRun(await res.json())
    } catch {}
    setRunning(false)
  }

  const humans = crew.roles.filter(r => r.human)
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex items-start justify-between cursor-pointer" onClick={() => setExpanded(!expanded)}>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="px-2 py-0.5 rounded text-xs border bg-stag-500/10 text-stag-400 border-stag-500/30">{crew.domain}</span>
            <span className="text-xs text-gray-500">{crew.roles.length} roles · {crew.workflow.length} steps · gate: {humans.map(h => h.title).join(', ')}</span>
          </div>
          <h3 className="font-medium text-sm">{crew.display_name}</h3>
          <p className="text-xs text-gray-400 mt-1 line-clamp-2">{crew.purpose}</p>
        </div>
        <div className="ml-2 text-gray-500">{expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</div>
      </div>

      {expanded && (
        <div className="mt-3 pt-3 border-t border-gray-800 space-y-3">
          <div>
            <h4 className="text-xs text-gray-500 uppercase mb-1">Roles</h4>
            <div className="flex flex-wrap gap-2">
              {crew.roles.map(r => (
                <span key={r.id} className={`px-2 py-1 rounded text-xs border ${r.human ? 'bg-orange-500/10 text-orange-300 border-orange-500/30' : 'bg-gray-800 text-gray-300 border-gray-700'}`}>
                  {r.human ? <UserCheck size={11} className="inline mr-1" /> : null}{r.title}{r.agent ? ` · ${r.agent}` : ''}
                </span>
              ))}
            </div>
          </div>
          <div>
            <h4 className="text-xs text-gray-500 uppercase mb-1">Workflow</h4>
            <div className="flex flex-wrap items-center gap-1 text-xs">
              {crew.workflow.map((s, i) => (
                <React.Fragment key={s.step}>
                  <span className={`font-mono ${STEP_COLORS[s.action] || 'text-gray-300'}`} title={`${s.action}${s.plan_step ? ' · ' + s.plan_step : ''}${s.policy ? ' · ' + s.policy.mode : ''}`}>{s.step}</span>
                  {i < crew.workflow.length - 1 && <ArrowRight size={10} className="text-gray-700" />}
                </React.Fragment>
              ))}
            </div>
          </div>
          <div>
            <h4 className="text-xs text-gray-500 uppercase mb-1">Skills</h4>
            <p className="text-xs text-gray-400 font-mono">{crew.skills.join(' · ')}</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => runCrew(false)} disabled={running} className="flex items-center gap-1 px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs border border-gray-700">
              <Play size={12} /> Dry run (halts at human gate)
            </button>
            <button onClick={() => runCrew(true)} disabled={running} className="flex items-center gap-1 px-3 py-1.5 rounded bg-stag-600 hover:bg-stag-500 text-xs">
              <UserCheck size={12} /> Dry run with approvals
            </button>
          </div>
          {run && (
            <div className="bg-gray-950 border border-gray-800 rounded p-3 text-xs space-y-1">
              <div className="flex flex-wrap gap-4 text-gray-300">
                <span>events {run.events_ingested}</span>
                <span>triggers {run.triggers_fired}</span>
                <span>duplicates {run.duplicate_deliveries}</span>
                <span>evidence-only {run.evidence_only_updates}</span>
                <span className="text-stag-400">model calls avoided {run.model_calls_avoided}</span>
                <span className="text-green-400">resolved {run.resolved}</span>
                <span className="text-orange-400">held {run.held}</span>
              </div>
              {run.incidents.map(inc => (
                <div key={inc.incident_id} className="mt-2">
                  <div className="text-gray-400">{inc.incident_id} · {inc.signal_key} · <span className={inc.final_state === 'resolved' ? 'text-green-400' : 'text-orange-400'}>{inc.final_state}</span></div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {inc.steps.map((s, i) => (
                      <span key={i} className={`px-1.5 py-0.5 rounded border ${s.status === 'done' ? 'border-gray-700 text-gray-300' : s.status === 'held' || s.status === 'awaiting' ? 'border-orange-500/40 text-orange-300' : 'border-gray-800 text-gray-500'}`} title={s.detail?.reason || s.action}>{s.step}</span>
                    ))}
                  </div>
                  {inc.halted_at && <div className="text-orange-400 mt-1">⏸ {inc.halt_reason}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function App() {
  // Same-origin WebSocket: works behind HTTPS/reverse proxies and the Vite dev proxy alike.
  const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/ws`
  const { connected, events, insights, incidentUpdates, lastError } = useWebSocket(wsUrl)
  const [stats, setStats] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState(null)
  const [activeTab, setActiveTab] = useState('insights')
  const [crews, setCrews] = useState([])

  // Load crews catalog once
  useEffect(() => {
    fetch('/api/crews').then(r => r.ok ? r.json() : { crews: [] }).then(d => setCrews(d.crews || [])).catch(() => {})
  }, [])

  // Poll stats
  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await fetch('/api/stats')
        if (res.ok) setStats(await res.json())
      } catch {}
    }
    fetchStats()
    const interval = setInterval(fetchStats, 5000)
    return () => clearInterval(interval)
  }, [])

  // Search handler
  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    try {
      const res = await fetch(`/api/knowledge/search?q=${encodeURIComponent(searchQuery)}&top_k=10`)
      if (res.ok) setSearchResults(await res.json())
    } catch {}
  }

  // Severity distribution for pie chart
  const severityCounts = insights.reduce((acc, o) => {
    const sev = o.insight?.severity || 'low'
    acc[sev] = (acc[sev] || 0) + 1
    return acc
  }, {})
  const pieData = Object.entries(severityCounts).map(([name, value]) => ({ name, value }))

  // Event rate for area chart (last 20 events grouped by 10s windows)
  const eventRateData = (() => {
    if (events.length === 0) return []
    const buckets = {}
    events.slice(0, 100).forEach(e => {
      const t = new Date(e.timestamp)
      const key = `${t.getHours()}:${String(t.getMinutes()).padStart(2, '0')}:${String(Math.floor(t.getSeconds() / 10) * 10).padStart(2, '0')}`
      buckets[key] = (buckets[key] || 0) + 1
    })
    return Object.entries(buckets).slice(-20).map(([time, count]) => ({ time, count }))
  })()

  return (
    <div className="min-h-screen bg-gray-950">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-950/90 backdrop-blur sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 bg-stag-600 rounded-lg flex items-center justify-center">
              <Layers size={18} className="text-white" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight">STAG {lastError && <span className="text-xs text-red-400 font-normal ml-2">{lastError}</span>}</h1>
              <p className="text-xs text-gray-500">Stream-Triggered Augmented Generation</p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <PipelineFlow stats={stats} />
            <div className="flex items-center gap-2">
              <Circle
                size={8}
                className={connected ? 'text-stag-400 fill-stag-400 animate-pulse-green' : 'text-red-400 fill-red-400'}
              />
              <span className="text-xs text-gray-500">{connected ? 'Live' : 'Disconnected'}</span>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 py-6 space-y-6">
        {/* Stats Row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard icon={Activity} label="Events" value={stats?.events_processed || 0} sub="total processed" />
          <StatCard icon={Zap} label="Triggers" value={stats?.triggers_fired || 0} sub={`${((stats?.trigger_rate || 0) * 100).toFixed(1)}% fire rate`} color="text-yellow-400" />
          <StatCard icon={Brain} label="Insights" value={stats?.insights_generated || 0} sub="generated" color="text-purple-400" />
          <StatCard icon={Database} label="Knowledge" value={stats?.retriever?.vector_count || 0} sub={`${stats?.retriever?.graph?.nodes || 0} graph nodes`} color="text-blue-400" />
        </div>

        {/* Charts Row */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="md:col-span-2 bg-gray-900 border border-gray-800 rounded-lg p-4">
            <h2 className="text-sm font-medium text-gray-400 mb-3">Event Rate</h2>
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={eventRateData}>
                <defs>
                  <linearGradient id="colorEvents" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3}/>
                    <stop offset="95%" stopColor="#22c55e" stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <XAxis dataKey="time" tick={{ fontSize: 10, fill: '#6b7280' }} />
                <YAxis tick={{ fontSize: 10, fill: '#6b7280' }} />
                <Tooltip
                  contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: '#9ca3af' }}
                />
                <Area type="monotone" dataKey="count" stroke="#22c55e" fillOpacity={1} fill="url(#colorEvents)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <h2 className="text-sm font-medium text-gray-400 mb-3">Severity Distribution</h2>
            {pieData.length > 0 ? (
              <ResponsiveContainer width="100%" height={180}>
                <PieChart>
                  <Pie data={pieData} cx="50%" cy="50%" innerRadius={40} outerRadius={70} dataKey="value" paddingAngle={2}>
                    {pieData.map((entry) => (
                      <Cell key={entry.name} fill={SEVERITY_COLORS[entry.name] || '#6b7280'} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-[180px] flex items-center justify-center text-gray-600 text-sm">Waiting for data...</div>
            )}
            <div className="flex flex-wrap gap-2 mt-2">
              {Object.entries(severityCounts).map(([sev, count]) => (
                <div key={sev} className="flex items-center gap-1 text-xs">
                  <div className="w-2 h-2 rounded-full" style={{ backgroundColor: SEVERITY_COLORS[sev] }} />
                  <span className="text-gray-400">{sev}: {count}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Search Bar */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                placeholder="Search knowledge base (e.g., 'ChatGPT source_code Engineering')"
                className="w-full bg-gray-800 border border-gray-700 rounded-lg pl-9 pr-4 py-2 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-stag-600"
              />
            </div>
            <button
              onClick={handleSearch}
              className="bg-stag-600 hover:bg-stag-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            >
              Search
            </button>
          </div>
          {searchResults && (
            <div className="mt-3 space-y-2 max-h-60 overflow-y-auto">
              <div className="text-xs text-gray-500">
                {searchResults.fused_results?.length || 0} results in {searchResults.retrieval_time_ms?.toFixed(0)}ms
                (vector: {searchResults.vector_results?.length}, graph: {searchResults.graph_results?.length}, sparse: {searchResults.sparse_results?.length})
              </div>
              {searchResults.fused_results?.map((r, i) => (
                <div key={i} className="bg-gray-800 rounded p-2 text-xs">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-stag-400 font-mono">{(r.score * 100).toFixed(0)}%</span>
                    {r.sources?.map(s => (
                      <span key={s} className="bg-gray-700 px-1.5 py-0.5 rounded text-gray-300">{s}</span>
                    ))}
                  </div>
                  <p className="text-gray-400 truncate">{r.text}</p>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Tabs: Insights / Events */}
        <div>
          <div className="flex gap-4 mb-4 border-b border-gray-800">
            <button
              onClick={() => setActiveTab('insights')}
              className={`pb-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === 'insights' ? 'border-stag-500 text-stag-400' : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              <div className="flex items-center gap-2">
                <AlertTriangle size={14} />
                Insights ({insights.length})
              </div>
            </button>
            <button
              onClick={() => setActiveTab('events')}
              className={`pb-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === 'events' ? 'border-stag-500 text-stag-400' : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              <div className="flex items-center gap-2">
                <Radio size={14} />
                Event Stream ({events.length})
              </div>
            </button>
            <button
              onClick={() => setActiveTab('crews')}
              className={`pb-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === 'crews' ? 'border-stag-500 text-stag-400' : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              <div className="flex items-center gap-2">
                <Users size={14} />
                Crews ({crews.length})
              </div>
            </button>
          </div>

          {activeTab === 'crews' ? (
            <div className="space-y-3">
              <p className="text-xs text-gray-500">
                <span className="px-1.5 py-0.5 rounded border border-orange-500/40 text-orange-300 mr-2">SIMULATION</span>
                Ready-made teams (roles + skills + workflow) that run on the TechOps backbone. Dry runs use the bundled
                scenario with fixture verdicts on an isolated coordinator and ledger; approvals are simulated flags and
                no action reaches a real destination.
              </p>
              {crews.length === 0 ? (
                <div className="text-center py-12 text-gray-600">
                  <Users size={32} className="mx-auto mb-2 opacity-50" />
                  <p>No crews found. Add one under <span className="font-mono">crews/&lt;name&gt;/crew.yaml</span>.</p>
                </div>
              ) : (
                crews.map(crew => <CrewCard key={crew.name} crew={crew} />)
              )}
            </div>
          ) : activeTab === 'insights' ? (
            <div className="space-y-3">
              {insights.length === 0 ? (
                <div className="text-center py-12 text-gray-600">
                  <Brain size={32} className="mx-auto mb-2 opacity-50" />
                  <p>Waiting for triggered insights...</p>
                  <p className="text-xs mt-1">Events are being processed. Insights appear when triggers fire.</p>
                </div>
              ) : (
                insights.map((output, i) => (
                  <InsightCard key={output.insight?.id || i} insight={output} defaultExpanded={i === 0} />
                ))
              )}
            </div>
          ) : (
            <div className="bg-gray-900 border border-gray-800 rounded-lg divide-y divide-gray-800/50 max-h-[500px] overflow-y-auto">
              {events.length === 0 ? (
                <div className="text-center py-12 text-gray-600">
                  <Radio size={32} className="mx-auto mb-2 opacity-50" />
                  <p>Waiting for events...</p>
                </div>
              ) : (
                events.map((event, i) => <EventRow key={event.id || i} event={event} />)
              )}
            </div>
          )}
        </div>
      </main>

      {/* Footer */}
      <footer className="border-t border-gray-800 mt-12 py-4 text-center text-xs text-gray-600">
        STAG v0.3.0 — Stream-Triggered Augmented Generation — Enterprise AI Tech · local demo (no auth); crew runs are simulations
      </footer>
    </div>
  )
}
