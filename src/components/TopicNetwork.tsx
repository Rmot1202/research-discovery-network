import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph3D, { type ForceGraph3DInstance } from '3d-force-graph'
import SpriteText from 'three-spritetext'
import * as THREE from 'three'
import type { GapTopic, Topic, TopicLink } from '../types'
import { gapColor } from './ResearchGaps'
import { ArrowIcon, SparkIcon } from './Icons'

type TopicNetworkProps = {
  topics: Topic[]
  links: TopicLink[]
  selectedId: string
  totalPapers: number
  isLoading: boolean
  hasPapers: boolean
  apiError: string
  onSelect: (topic: Topic) => void
  onClear: () => void
  onAsk?: (topic: Topic) => void
  gapByCode?: Record<string, GapTopic>
}

type BrainNode = {
  id: string
  label: string
  count: number
  region: RegionKey
  x?: number
  y?: number
  z?: number
  vx?: number
  vy?: number
  vz?: number
}
type BrainLink = { source: string | BrainNode; target: string | BrainNode; shared_papers: number; strength: number }
type RegionKey = 'learning' | 'theory' | 'software' | 'systems' | 'human' | 'security'

// Brain dimensions (left–right, up–down, front–back), like a top-down connectome scan.
const BRAIN = { rx: 120, ry: 78, rz: 150 }

const REGIONS: Record<RegionKey, { name: string; color: string; center: [number, number, number] }> = {
  learning: { name: 'AI & learning', color: '#ff4f64', center: [0, 18, 70] },
  theory: { name: 'Theory & math', color: '#3fe08f', center: [-64, 0, -6] },
  software: { name: 'Software & data', color: '#36d6ec', center: [64, 4, -4] },
  systems: { name: 'Systems & hardware', color: '#5671ff', center: [0, -6, -96] },
  human: { name: 'People, media & society', color: '#e46bff', center: [0, 44, -10] },
  security: { name: 'Security', color: '#ffb23f', center: [0, -34, 22] },
}

const REGION_BY_CODE: Record<string, RegionKey> = {
  'cs.AI': 'learning', 'cs.LG': 'learning', 'cs.CL': 'learning', 'cs.CV': 'learning', 'cs.NE': 'learning',
  'cs.MA': 'learning', 'cs.IR': 'learning',
  'cs.CC': 'theory', 'cs.CG': 'theory', 'cs.DM': 'theory', 'cs.DS': 'theory', 'cs.FL': 'theory', 'cs.IT': 'theory',
  'cs.LO': 'theory', 'cs.GT': 'theory', 'cs.NA': 'theory', 'cs.SC': 'theory', 'cs.MS': 'theory',
  'cs.SE': 'software', 'cs.PL': 'software', 'cs.DB': 'software', 'cs.DL': 'software', 'cs.CE': 'software',
  'cs.AR': 'systems', 'cs.DC': 'systems', 'cs.OS': 'systems', 'cs.NI': 'systems', 'cs.PF': 'systems',
  'cs.ET': 'systems', 'cs.SY': 'systems', 'cs.RO': 'systems',
  'cs.HC': 'human', 'cs.CY': 'human', 'cs.SI': 'human', 'cs.GR': 'human', 'cs.MM': 'human', 'cs.SD': 'human',
  'cs.GL': 'human', 'cs.OH': 'human',
  'cs.CR': 'security',
}

const linkEndId = (end: string | BrainNode) => (typeof end === 'string' ? end : end.id)

function seededRandom(seed: number) {
  let value = seed
  return () => {
    value = (value * 16807) % 2147483647
    return (value - 1) / 2147483646
  }
}

function buildBrainShell(): THREE.Group {
  const group = new THREE.Group()
  group.name = 'brain-shell'
  const shellMaterial = new THREE.MeshPhongMaterial({
    color: '#bfe9e2', transparent: true, opacity: 0.07, depthWrite: false, side: THREE.DoubleSide, shininess: 60,
  })
  const rimMaterial = new THREE.MeshBasicMaterial({
    color: '#8fd8cc', wireframe: true, transparent: true, opacity: 0.035, depthWrite: false,
  })
  for (const side of [-1, 1]) {
    const geometry = new THREE.SphereGeometry(1, 48, 32)
    // Flatten the inner face of each hemisphere so the pair reads as a brain from above.
    const position = geometry.attributes.position
    for (let index = 0; index < position.count; index += 1) {
      const x = position.getX(index)
      const y = position.getY(index)
      const z = position.getZ(index)
      const inner = x * side < 0 ? 0.72 : 1
      const frontNarrow = 1 - Math.max(0, z) * 0.12
      position.setXYZ(index, x * inner * frontNarrow, y * (1 - Math.max(0, -y) * 0.25), z)
    }
    geometry.computeVertexNormals()
    for (const material of [shellMaterial, rimMaterial]) {
      const mesh = new THREE.Mesh(geometry, material)
      mesh.scale.set(BRAIN.rx * 0.56, BRAIN.ry, BRAIN.rz)
      mesh.position.set(side * BRAIN.rx * 0.47, 0, 0)
      mesh.renderOrder = -1
      group.add(mesh)
    }
  }

  // Faint "white-matter" fibres, inspired by tractography renders.
  const random = seededRandom(42)
  const points: number[] = []
  const colors: number[] = []
  const palette = [new THREE.Color('#d9893a'), new THREE.Color('#1f9e86')]
  for (let fibre = 0; fibre < 520; fibre += 1) {
    const theta = random() * Math.PI * 2
    const phi = Math.acos(2 * random() - 1)
    const radius = 0.25 + random() * 0.65
    const start = new THREE.Vector3(
      Math.sin(phi) * Math.cos(theta) * BRAIN.rx * radius,
      Math.cos(phi) * BRAIN.ry * radius * 0.85,
      Math.sin(phi) * Math.sin(theta) * BRAIN.rz * radius,
    )
    const direction = new THREE.Vector3(random() - 0.5, (random() - 0.5) * 0.5, random() - 0.5).normalize()
    let previous = start
    const color = palette[fibre % 2]
    for (let step = 0; step < 4; step += 1) {
      direction.add(new THREE.Vector3((random() - 0.5) * 0.6, (random() - 0.5) * 0.3, (random() - 0.5) * 0.6)).normalize()
      const next = previous.clone().addScaledVector(direction, 9 + random() * 8)
      points.push(previous.x, previous.y, previous.z, next.x, next.y, next.z)
      colors.push(color.r, color.g, color.b, color.r, color.g, color.b)
      previous = next
    }
  }
  const fibreGeometry = new THREE.BufferGeometry()
  fibreGeometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3))
  fibreGeometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3))
  const fibres = new THREE.LineSegments(
    fibreGeometry,
    new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.16, depthWrite: false }),
  )
  group.add(fibres)
  return group
}

// Custom d3 forces: keep every node inside the brain and near its research region.
function brainContainmentForce() {
  let nodes: BrainNode[] = []
  const force = (alpha: number) => {
    for (const node of nodes) {
      const x = node.x ?? 0
      const y = node.y ?? 0
      const z = node.z ?? 0
      const distance = Math.hypot(x / BRAIN.rx, y / BRAIN.ry, z / BRAIN.rz)
      if (distance > 0.82) {
        const push = (distance - 0.82) * alpha * 1.4
        node.vx = (node.vx ?? 0) - x * push
        node.vy = (node.vy ?? 0) - y * push
        node.vz = (node.vz ?? 0) - z * push
      }
    }
  }
  force.initialize = (initial: BrainNode[]) => { nodes = initial }
  return force
}

function regionGravityForce() {
  let nodes: BrainNode[] = []
  const force = (alpha: number) => {
    for (const node of nodes) {
      const [cx, cy, cz] = REGIONS[node.region].center
      node.vx = (node.vx ?? 0) + (cx - (node.x ?? 0)) * 0.06 * alpha
      node.vy = (node.vy ?? 0) + (cy - (node.y ?? 0)) * 0.06 * alpha
      node.vz = (node.vz ?? 0) + (cz - (node.z ?? 0)) * 0.06 * alpha
    }
  }
  force.initialize = (initial: BrainNode[]) => { nodes = initial }
  return force
}

export default function TopicNetwork({
  topics,
  links,
  selectedId,
  totalPapers,
  isLoading,
  hasPapers,
  apiError,
  onSelect,
  onClear,
  onAsk,
  gapByCode = {},
}: TopicNetworkProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const graphRef = useRef<ForceGraph3DInstance | null>(null)
  const nodeCache = useRef(new Map<string, BrainNode>())
  const selectedRef = useRef(selectedId)
  const onSelectRef = useRef(onSelect)
  const topicsRef = useRef(topics)
  const maxCountRef = useRef(1)
  const hasFramedRef = useRef(false)
  const tourRef = useRef<number | null>(null)
  const [webglError, setWebglError] = useState('')
  const [isTouring, setIsTouring] = useState(false)
  const [hovered, setHovered] = useState<BrainNode | null>(null)
  const [colorMode, setColorMode] = useState<'regions' | 'gaps'>('regions')
  const hasGaps = Object.keys(gapByCode).length > 0
  const reducedMotion = useMemo(
    () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches,
    [],
  )

  selectedRef.current = selectedId
  onSelectRef.current = onSelect
  topicsRef.current = topics

  const maxTopicCount = Math.max(1, ...topics.map((topic) => topic.count))
  maxCountRef.current = maxTopicCount
  const maxLinkWeight = Math.max(1, ...links.map((link) => link.shared_papers))
  const selected = topics.find((topic) => topic.code === selectedId)

  const neighbours = useMemo(() => {
    if (!selectedId) return []
    return links
      .filter((link) => link.source === selectedId || link.target === selectedId)
      .map((link) => ({ code: link.source === selectedId ? link.target : link.source, shared: link.shared_papers }))
      .sort((left, right) => right.shared - left.shared)
      .slice(0, 6)
  }, [links, selectedId])

  const flyTo = useCallback((code: string, duration = 1600) => {
    const graph = graphRef.current
    const node = nodeCache.current.get(code)
    if (!graph || !node || node.x === undefined) return
    const x = node.x ?? 0
    const y = node.y ?? 0
    const z = node.z ?? 0
    const length = Math.hypot(x, y, z) || 1
    // Approach from outside the node, looking at it, so the camera travels through the brain.
    const distance = 95
    graph.cameraPosition(
      { x: x + (x / length) * distance, y: y + (y / length) * distance + 12, z: z + (z / length) * distance },
      { x, y, z },
      reducedMotion ? 0 : duration,
    )
  }, [reducedMotion])

  const stopTour = useCallback(() => {
    if (tourRef.current !== null) window.clearInterval(tourRef.current)
    tourRef.current = null
    setIsTouring(false)
  }, [])

  // Create the 3D scene once.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    let graph: ForceGraph3DInstance
    try {
      graph = new ForceGraph3D(container, { controlType: 'orbit' })
    } catch (error) {
      setWebglError(error instanceof Error ? error.message : '3D graphics are unavailable in this browser.')
      return
    }
    graphRef.current = graph
    graph
      .backgroundColor('rgba(0,0,0,0)')
      .showNavInfo(false)
      .nodeId('id')
      .nodeRelSize(4)
      .nodeResolution(24)
      .nodeOpacity(0.96)
      .nodeLabel((node) => {
        const item = node as BrainNode
        return `<div class="graph-tooltip"><b>${item.label}</b><span>${item.id} · ${item.count.toLocaleString()} papers</span><em>${REGIONS[item.region].name}</em></div>`
      })
      .nodeThreeObjectExtend(true)
      .nodeThreeObject((node) => {
        const item = node as BrainNode
        const sprite = new SpriteText(item.id.replace('cs.', ''))
        sprite.color = '#f2fffb'
        sprite.textHeight = 5.2
        sprite.fontWeight = '700'
        sprite.backgroundColor = 'rgba(6,24,30,0.55)'
        sprite.padding = 1.4
        sprite.borderRadius = 3
        const radius = Math.cbrt(1 + 38 * Math.sqrt(item.count / maxCountRef.current)) * 4
        sprite.position.set(0, radius + 5, 0)
        sprite.material.depthWrite = false
        return sprite
      })
      .linkOpacity(0.42)
      .linkDirectionalParticleSpeed(0.0045)
      .onNodeHover((node) => {
        container.style.cursor = node ? 'pointer' : 'grab'
        setHovered((node as BrainNode | null) ?? null)
      })
      .onNodeClick((node) => {
        const item = node as BrainNode
        stopTour()
        const topic = topicsRef.current.find((entry) => entry.code === item.id)
        if (topic) onSelectRef.current(topic)
        flyTo(item.id)
      })
      .onBackgroundClick(() => stopTour())
      .cooldownTicks(220)
      .onEngineStop(() => {
        if (!hasFramedRef.current) {
          hasFramedRef.current = true
          graph.zoomToFit(reducedMotion ? 0 : 1200, 60)
        }
      })
      .d3VelocityDecay(0.35)

    graph.d3Force('brain', brainContainmentForce() as never)
    graph.d3Force('region', regionGravityForce() as never)
    graph.d3Force('center', null as never)
    const charge = graph.d3Force('charge') as { strength?: (value: number) => void } | undefined
    charge?.strength?.(-90)
    const linkForce = graph.d3Force('link') as {
      distance?: (value: (link: BrainLink) => number) => void
      strength?: (value: (link: BrainLink) => number) => void
    } | undefined
    linkForce?.distance?.((link) => 70 - link.strength * 30)
    linkForce?.strength?.((link) => 0.02 + link.strength * 0.08)

    const scene = graph.scene()
    scene.add(buildBrainShell())
    scene.add(new THREE.AmbientLight('#9fd6cc', 1.1))
    const rim = new THREE.DirectionalLight('#ffffff', 1.4)
    rim.position.set(120, 200, 160)
    scene.add(rim)
    scene.fog = new THREE.FogExp2('#071c24', 0.0016)

    const controls = graph.controls() as { autoRotate?: boolean; autoRotateSpeed?: number; target?: THREE.Vector3; update?: () => void; addEventListener?: (name: string, cb: () => void) => void }
    if (controls) {
      controls.autoRotate = !reducedMotion
      controls.autoRotateSpeed = 0.55
      controls.addEventListener?.('start', () => { controls.autoRotate = false })
    }
    graph.cameraPosition({ x: 0, y: 330, z: 150 }, { x: 0, y: 0, z: 0 })

    const resize = () => {
      graph.width(container.clientWidth).height(container.clientHeight)
    }
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(container)

    // Keyboard flight: W/S forward/back, A/D strafe, Q/E down/up, arrow keys also work.
    const keyHandler = (event: KeyboardEvent) => {
      const camera = graph.camera() as THREE.PerspectiveCamera
      const step = event.shiftKey ? 18 : 7
      const forward = new THREE.Vector3()
      camera.getWorldDirection(forward)
      const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize()
      const move = new THREE.Vector3()
      switch (event.key.toLowerCase()) {
        case 'w': case 'arrowup': move.addScaledVector(forward, step); break
        case 's': case 'arrowdown': move.addScaledVector(forward, -step); break
        case 'a': case 'arrowleft': move.addScaledVector(right, -step); break
        case 'd': case 'arrowright': move.addScaledVector(right, step); break
        case 'e': move.y += step; break
        case 'q': move.y -= step; break
        default: return
      }
      event.preventDefault()
      stopTour()
      if (controls) controls.autoRotate = false
      camera.position.add(move)
      controls?.target?.add(move)
      controls?.update?.()
    }
    container.addEventListener('keydown', keyHandler)

    return () => {
      observer.disconnect()
      container.removeEventListener('keydown', keyHandler)
      graph._destructor()
      container.innerHTML = ''
      graphRef.current = null
    }
  }, [flyTo, reducedMotion, stopTour])

  // Push topic data into the scene, reusing node objects so positions are stable.
  useEffect(() => {
    const graph = graphRef.current
    if (!graph) return
    const nodes: BrainNode[] = topics.map((topic) => {
      const existing = nodeCache.current.get(topic.code)
      const region = REGION_BY_CODE[topic.code] ?? 'human'
      if (existing) {
        existing.count = topic.count
        existing.label = topic.label
        return existing
      }
      const [cx, cy, cz] = REGIONS[region].center
      const node: BrainNode = {
        id: topic.code, label: topic.label, count: topic.count, region,
        x: cx + (Math.random() - 0.5) * 30, y: cy + (Math.random() - 0.5) * 20, z: cz + (Math.random() - 0.5) * 30,
      }
      nodeCache.current.set(topic.code, node)
      return node
    })
    const known = new Set(nodes.map((node) => node.id))
    const graphLinks: BrainLink[] = links
      .filter((link) => known.has(link.source) && known.has(link.target))
      .map((link) => ({ ...link, strength: Math.log1p(link.shared_papers) / Math.log1p(maxLinkWeight) }))
    graph
      .nodeVal((node) => 1 + 38 * Math.sqrt((node as BrainNode).count / maxTopicCount))
      .graphData({ nodes, links: graphLinks })
  }, [links, maxLinkWeight, maxTopicCount, topics])

  // Re-style nodes and links when the selection changes.
  useEffect(() => {
    const graph = graphRef.current
    if (!graph) return
    const related = new Set<string>([selectedId])
    for (const link of links) {
      if (link.source === selectedId) related.add(link.target)
      if (link.target === selectedId) related.add(link.source)
    }
    const isRelated = (link: BrainLink) => linkEndId(link.source) === selectedId || linkEndId(link.target) === selectedId
    const baseColor = (item: BrainNode) => {
      const gap = gapByCode[item.id]
      if (colorMode !== 'gaps' || !gap) return REGIONS[item.region].color
      return gap.metrics.papers === 0 ? '#5b6f73' : gapColor(gap.index)
    }
    graph
      .nodeColor((node) => {
        const item = node as BrainNode
        if (item.id === selectedId) return '#ffffff'
        if (selectedId && !related.has(item.id)) return new THREE.Color(baseColor(item)).multiplyScalar(0.45).getStyle()
        return baseColor(item)
      })
      .linkColor((link) => (isRelated(link as BrainLink) ? '#ffd27a' : selectedId ? 'rgba(150,170,175,0.35)' : '#9aa9ad'))
      .linkWidth((link) => {
        const item = link as BrainLink
        return (isRelated(item) ? 1.4 : 0.35) + item.strength * (isRelated(item) ? 2.4 : 1.4)
      })
      .linkDirectionalParticles((link) => {
        if (reducedMotion) return 0
        const item = link as BrainLink
        if (selectedId) return isRelated(item) ? 3 : 0
        return item.strength > 0.55 ? 2 : 0
      })
      .linkDirectionalParticleWidth((link) => (isRelated(link as BrainLink) ? 2.4 : 1.4))
      .linkDirectionalParticleColor((link) => (isRelated(link as BrainLink) ? '#fff2c4' : '#9effe3'))
  }, [colorMode, gapByCode, links, reducedMotion, selectedId])

  useEffect(() => () => stopTour(), [stopTour])

  const startTour = () => {
    if (isTouring) { stopTour(); return }
    const order: string[] = []
    const start = selectedRef.current || [...topics].sort((a, b) => b.count - a.count)[0]?.code
    if (!start) return
    // Walk the network along its strongest links, like a camera flying through the connectome.
    const visited = new Set<string>()
    let current: string | undefined = start
    while (current && order.length < Math.min(14, topics.length)) {
      order.push(current)
      visited.add(current)
      const here: string = current
      const next = links
        .filter((link) => (link.source === here && !visited.has(link.target)) || (link.target === here && !visited.has(link.source)))
        .sort((a, b) => b.shared_papers - a.shared_papers)[0]
      current = next ? (next.source === here ? next.target : next.source)
        : topics.find((topic) => !visited.has(topic.code))?.code
    }
    const controls = graphRef.current?.controls() as { autoRotate?: boolean } | undefined
    if (controls) controls.autoRotate = false
    setIsTouring(true)
    let index = 0
    const visit = () => {
      const code = order[index % order.length]
      const topic = topics.find((entry) => entry.code === code)
      if (topic && topic.code !== selectedRef.current) onSelectRef.current(topic)
      flyTo(code, 2600)
      index += 1
    }
    visit()
    tourRef.current = window.setInterval(visit, 3600)
  }

  const resetView = () => {
    stopTour()
    graphRef.current?.zoomToFit(reducedMotion ? 0 : 1400, 60)
  }

  return (
    <div className="map-shell" id="topics-map" aria-label="Interactive 3D arXiv computer science topic map">
      <div className="map-toolbar">
        <div><span className="live-dot" /><strong>Research signal · 3D topic brain</strong><small>{isLoading ? 'Loading…' : `${totalPapers.toLocaleString()} papers`}</small></div>
        <div className="map-controls">
          {hasGaps && (
            <div className="color-toggle" role="group" aria-label="Color nodes by">
              <span>Color</span>
              <button aria-pressed={colorMode === 'regions'} onClick={() => setColorMode('regions')}>Regions</button>
              <button aria-pressed={colorMode === 'gaps'} onClick={() => setColorMode('gaps')}>Gap index</button>
            </div>
          )}
          <button onClick={startTour} aria-pressed={isTouring}>{isTouring ? 'Stop tour' : 'Fly-through tour'}</button>
          <button onClick={resetView}>Reset view</button>
        </div>
      </div>
      <div className="network network-3d">
        <div
          ref={containerRef}
          className="graph-canvas"
          tabIndex={0}
          aria-label="3D topic network. Drag to rotate, scroll to zoom, W A S D to fly, click a node to travel to it."
        />
        {webglError && <div className="api-status error" role="status">3D view unavailable: {webglError}</div>}
        {apiError && <div className="api-status error" role="status">{apiError}</div>}
        {isLoading && !hasPapers && <div className="api-status" role="status">Loading arXiv topics and papers…</div>}
        {hovered && !selected && (
          <div className="hover-chip"><i style={{ background: REGIONS[hovered.region].color }} />{hovered.label} · {hovered.count.toLocaleString()}</div>
        )}
        {colorMode === 'gaps' && hasGaps ? (
          <div className="region-legend gap-legend" aria-label="Under-researched index colour scale">
            <span className="gap-legend-title">Under-researched index</span>
            <div className="gap-legend-bar" />
            <div className="gap-legend-ends"><small>Well developed</small><small>Under-researched</small></div>
            <small className="gap-legend-none"><i /> Grey: no imported papers yet</small>
          </div>
        ) : (
          <div className="region-legend" aria-label="Research regions">
            {Object.values(REGIONS).map((region) => (
              <span key={region.name}><i style={{ background: region.color }} />{region.name}</span>
            ))}
          </div>
        )}
        <div className="map-hint">
          <span>{topics.length} categories</span><i />
          Drag to rotate · scroll to zoom · W A S D / Q E to fly · click a node to travel
        </div>
      </div>
      {selected && (
        <aside className="detail-panel">
          <button className="close" onClick={() => { stopTour(); onClear() }} aria-label="Clear selected topic">×</button>
          <span className="panel-label">arXiv topic · {selected.code}</span>
          <h2>{selected.label}</h2>
          <div className="representation">
            <div><span>Imported papers</span><strong>{selected.count.toLocaleString()}</strong></div>
            <div className="score-track"><i style={{ width: `${selected.count ? Math.max(4, selected.count / maxTopicCount * 100) : 0}%` }} /></div>
            <small>Sphere size is proportional to the square root of the paper count.</small>
          </div>
          {gapByCode[selected.code] && gapByCode[selected.code].metrics.papers > 0 && (() => {
            const gap = gapByCode[selected.code]
            const rep = gap.representation
            return (
              <div className="panel-gaps">
                <div>
                  <span>Under-researched</span>
                  <strong style={{ color: gapColor(gap.index) }}>{gap.index.toFixed(0)}</strong>
                  <small>{gap.verdict}{gap.metrics.papers ? ` · ${gap.drivers.join(', ')}` : ''}</small>
                </div>
                <div>
                  <span>Underrepresented</span>
                  <strong style={{ color: rep.index === null ? '#7f9798' : gapColor(rep.index) }}>{rep.index === null ? 'n/a' : rep.index.toFixed(0)}</strong>
                  <small>{rep.index === null ? `${rep.documented_pct}% of papers have documented affiliations` : rep.verdict}</small>
                </div>
                <a href="#gaps">How these are scored</a>
              </div>
            )
          })()}
          {neighbours.length > 0 && (
            <div className="neighbour-list">
              <span className="panel-label">Travel to a connected topic</span>
              {neighbours.map((neighbour) => {
                const topic = topics.find((entry) => entry.code === neighbour.code)
                if (!topic) return null
                return (
                  <button key={neighbour.code} onClick={() => { stopTour(); onSelect(topic); flyTo(topic.code) }}>
                    <i style={{ background: REGIONS[REGION_BY_CODE[topic.code] ?? 'human'].color }} />
                    <span>{topic.label}</span>
                    <small>{neighbour.shared.toLocaleString()} shared</small>
                  </button>
                )
              })}
            </div>
          )}
          <button className="panel-action" onClick={() => document.getElementById('papers')?.scrollIntoView({ behavior: 'smooth' })}>
            View papers in this topic <ArrowIcon />
          </button>
          {onAsk && (
            <button className="panel-action ask-action" onClick={() => onAsk(selected)}>
              <SparkIcon /> Ask the research assistant
            </button>
          )}
        </aside>
      )}
    </div>
  )
}
