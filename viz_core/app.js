const TAG_FILES = [
  { file: 'people.yaml', label: 'People', color: '#e06c5a' },
  { file: 'organizations.yaml', label: 'Organizations', color: '#5b9bd5' },
  { file: 'places.yaml', label: 'Places', color: '#4ec89e' },
  { file: 'events_actions.yaml', label: 'Events & Actions', color: '#a87bce' },
  { file: 'creative_works_media.yaml', label: 'Creative Works & Media', color: '#e0974a' },
  { file: 'products_artifacts.yaml', label: 'Products & Artifacts', color: '#45bfb0' },
  { file: 'science.yaml', label: 'Science', color: '#7a9eb8' },
  { file: 'knowledge_information_abstracts.yaml', label: 'Knowledge & Abstracts', color: '#b8782d' },
];

// ── DOM refs ──
const fileListEl = document.getElementById('fileList');
const fileFilterEl = document.getElementById('fileFilter');
const fileTitleEl = document.getElementById('fileTitle');
const fileMetaEl = document.getElementById('fileMeta');
const overviewViewEl = document.getElementById('overviewView');
const sunburstViewEl = document.getElementById('sunburstView');
const graphViewEl = document.getElementById('graphView');
const schemaViewEl = document.getElementById('schemaView');
const hierarchyViewEl = document.getElementById('hierarchyView');
const fileViewEl = document.getElementById('fileView');
const summaryPanelEl = document.getElementById('summaryPanel');
const treePanelEl = document.getElementById('treePanel');
const rawPanelEl = document.getElementById('rawPanel');
const rawCodeEl = document.getElementById('rawCode');
const treeRootEl = document.getElementById('treeRoot');
const overviewContentEl = document.getElementById('overviewContent');
const sunburstContentEl = document.getElementById('sunburstContent');
const graphContentEl = document.getElementById('graphContent');
const schemaChordEl = document.getElementById('schemaChord');
const schemaDetailEl = document.getElementById('schemaDetail');
const schemaFilterEl = document.getElementById('schemaFilter');
const schemaResetBtn = document.getElementById('schemaReset');
const hierarchyContentEl = document.getElementById('hierarchyContent');
const refreshBtn = document.getElementById('refreshBtn');
const expandAllBtn = document.getElementById('expandAll');
const collapseAllBtn = document.getElementById('collapseAll');
const sunburstResetBtn = document.getElementById('sunburstReset');
const graphResetBtn = document.getElementById('graphReset');

const viewToggle = document.getElementById('viewToggle');
const viewToggleButtons = viewToggle ? Array.from(viewToggle.querySelectorAll('button')) : [];

let currentFile = null;
let currentView = 'overview';
let allData = {};
let entityCounts = null;
let idLabels = null;
let sunburstChart = null;
let graphChart = null;
let schemaChart = null;
let schemaData = null;
let hierarchyChart = null;

// ── Init ──
async function init() {
  renderFileList(TAG_FILES);
  await Promise.all([loadAllFiles(), loadEntityCounts(), loadLabels(), loadSchemaData()]);
  renderOverview();

  // Navigation
  document.querySelectorAll('#mainNav .nav-pill').forEach(btn => {
    btn.addEventListener('click', () => switchMainView(btn.dataset.view));
  });

  fileFilterEl.addEventListener('input', handleFilter);

  refreshBtn.addEventListener('click', async () => {
    await loadAllFiles();
    renderOverview();
    hierarchyChart = null;
    schemaChart = null;
    if (currentView === 'sunburst') renderSunburst();
    if (currentView === 'hierarchy') renderHierarchy();
    if (currentView === 'graph') renderGraph();
    if (currentView === 'schema') renderSchema();
    if (currentFile) loadFile(currentFile);
  });

  viewToggleButtons.forEach(button => {
    button.addEventListener('click', () => setActivePanel(button.dataset.view));
  });

  expandAllBtn.addEventListener('click', () => toggleAllNodes(treeRootEl, true));
  collapseAllBtn.addEventListener('click', () => toggleAllNodes(treeRootEl, false));

  sunburstResetBtn.addEventListener('click', () => {
    if (sunburstChart) sunburstChart.reset();
  });

  graphResetBtn.addEventListener('click', () => renderGraph());

  schemaResetBtn.addEventListener('click', () => {
    schemaFilterEl.value = 'all';
    schemaChart = null;
    renderSchema();
  });
  schemaFilterEl.addEventListener('change', () => {
    schemaChart = null;
    renderSchema();
  });

  window.addEventListener('resize', debounce(() => {
    if (currentView === 'sunburst') renderSunburst();
    if (currentView === 'graph') renderGraph();
    if (currentView === 'schema') { schemaChart = null; renderSchema(); }
    if (currentView === 'hierarchy') renderHierarchy();
  }, 250));
}

function debounce(fn, delay) {
  let timeout;
  return (...args) => {
    clearTimeout(timeout);
    timeout = setTimeout(() => fn(...args), delay);
  };
}

function switchMainView(view) {
  currentView = view;
  currentFile = null;

  // Update nav pills
  document.querySelectorAll('#mainNav .nav-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  document.querySelectorAll('.file-button').forEach(btn => btn.classList.remove('active'));

  // Toggle views
  overviewViewEl.classList.toggle('hidden', view !== 'overview');
  sunburstViewEl.classList.toggle('hidden', view !== 'sunburst');
  graphViewEl.classList.toggle('hidden', view !== 'graph');
  schemaViewEl.classList.toggle('hidden', view !== 'schema');
  hierarchyViewEl.classList.toggle('hidden', view !== 'hierarchy');
  fileViewEl.classList.toggle('hidden', view !== 'file');

  if (view === 'sunburst' && !sunburstChart) renderSunburst();
  if (view === 'graph' && !graphChart) renderGraph();
  if (view === 'schema' && !schemaChart) renderSchema();
  if (view === 'hierarchy' && !hierarchyChart) renderHierarchy();
}

// ── Data loading ──
async function loadAllFiles() {
  const promises = TAG_FILES.map(async (entry) => {
    try {
      const response = await fetch(`../src/tags/${entry.file}`);
      if (!response.ok) throw new Error(`Failed to load ${entry.file}`);
      const text = await response.text();
      const data = jsyaml.load(text);
      return { file: entry.file, label: entry.label, color: entry.color, data, text };
    } catch (error) {
      console.error(`Error loading ${entry.file}:`, error);
      return { file: entry.file, label: entry.label, color: entry.color, data: null, text: '', error };
    }
  });
  const results = await Promise.all(promises);
  results.forEach(result => { allData[result.file] = result; });
}

async function loadLabels() {
  try {
    const response = await fetch(`labels.json?t=${Date.now()}`);
    if (!response.ok) throw new Error('Failed to load labels.json');
    idLabels = await response.json();
  } catch (error) {
    console.error('Labels not available:', error);
    idLabels = {};
  }
}

function labelFor(id) { return idLabels?.[id] || ''; }

function idWithLabel(id) {
  const label = labelFor(id);
  return label ? `${id} (${label})` : id;
}

async function loadEntityCounts() {
  try {
    const response = await fetch('category_counts.json');
    if (!response.ok) throw new Error('Failed to load category_counts.json');
    entityCounts = await response.json();
  } catch (error) {
    console.error('Entity counts not available:', error);
    entityCounts = null;
  }
}

// ============================================
// HELPERS
// ============================================
function formatCount(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toLocaleString();
}

function formatCountShort(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'K';
  return n.toLocaleString();
}

// ============================================
// PIE CHART
// ============================================
function renderSunburst() {
  if (!entityCounts) {
    sunburstContentEl.innerHTML = '<div class="empty">Entity counts not loaded.</div>';
    return;
  }
  renderPieCategories();
  sunburstChart = { reset: () => renderPieCategories() };
}

function renderPieCategories() {
  const container = sunburstContentEl;
  container.innerHTML = '';

  document.getElementById('pieTitle').textContent = 'Entity Distribution';
  document.getElementById('pieMeta').textContent = `${entityCounts.total.toLocaleString()} total entities \u2014 click a slice to see modules`;

  const entries = Object.values(allData).filter(e => e.data);
  const slices = entries.map(entry => {
    const catKey = entry.file.replace('.yaml', '');
    const catData = entityCounts.categories[catKey];
    return {
      name: entry.label,
      value: catData?.count || 0,
      color: entry.color,
      file: entry.file,
      noModule: catData?.no_module || 0,
      modules: catData?.modules || {}
    };
  }).filter(d => d.value > 0).sort((a, b) => b.value - a.value);

  if (entityCounts.unclassified > 0) {
    slices.push({ name: 'Unclassified', value: entityCounts.unclassified, color: '#a09a93', file: null, noModule: 0, modules: {} });
  }

  renderPie(container, slices, entityCounts.total, (d) => {
    if (d.data.file) renderPieModules(d.data);
  });
}

function renderPieModules(catData) {
  const container = sunburstContentEl;
  container.innerHTML = '';

  document.getElementById('pieTitle').textContent = catData.name;
  document.getElementById('pieMeta').textContent = `${catData.value.toLocaleString()} entities \u2014 click center to go back`;

  // Split modules into intrinsic vs relational using YAML type field
  const yamlEntry = allData[catData.file];
  const yamlModules = yamlEntry?.data?.modules || {};

  const intrinsic = [];
  const relational = [];

  Object.entries(catData.modules).forEach(([name, count]) => {
    if (count <= 0) return;
    const modType = yamlModules[name]?.type || 'intrinsic';
    if (modType === 'relational') {
      relational.push({ name, value: count });
    } else {
      intrinsic.push({ name, value: count, color: catData.color });
    }
  });

  intrinsic.sort((a, b) => b.value - a.value);
  relational.sort((a, b) => b.value - a.value);

  // Layout: intrinsic pie on the left, relational bars on the right
  const wrapper = document.createElement('div');
  wrapper.style.cssText = 'display:flex; gap:32px; align-items:flex-start; flex-wrap:wrap;';
  container.appendChild(wrapper);

  // ── Intrinsic pie ──
  const pieWrap = document.createElement('div');
  pieWrap.style.cssText = 'flex:1; min-width:300px;';
  const pieHeader = document.createElement('div');
  pieHeader.innerHTML = `<h3 style="font-family:'Instrument Serif',serif; font-size:18px; margin:0 0 8px; color:#1a1a1a">Intrinsic modules</h3>
    <p style="font-size:12px; color:#a09a93; margin:0 0 12px">Exclusive category partitions</p>`;
  pieWrap.appendChild(pieHeader);
  wrapper.appendChild(pieWrap);

  const palette = [
    '#e06c5a', '#5b9bd5', '#4ec89e', '#a87bce', '#e0974a',
    '#45bfb0', '#7a9eb8', '#b8782d', '#c7697a', '#6bb5d5',
    '#8fbc5a', '#d4826a', '#7c6fb0', '#c9a83e', '#5aab8c',
    '#d17ba5', '#6a9e5b', '#b09474', '#5e8cc9', '#c4625e',
    '#43a8a0', '#9b7dc4', '#d19e56', '#7eb87a'
  ];
  intrinsic.forEach((s, i) => { s.color = palette[i % palette.length]; });

  if (catData.noModule > 0) {
    intrinsic.push({ name: 'no module', value: catData.noModule, color: '#ddd7ce' });
  }

  renderPie(pieWrap, intrinsic, catData.value, null);

  // ── Relational bar chart ──
  if (relational.length > 0) {
    const barWrap = document.createElement('div');
    barWrap.style.cssText = 'flex:1; min-width:300px;';
    const barHeader = document.createElement('div');
    barHeader.innerHTML = `<h3 style="font-family:'Instrument Serif',serif; font-size:18px; margin:0 0 8px; color:#1a1a1a">Relational modules</h3>
      <p style="font-size:12px; color:#a09a93; margin:0 0 12px">Cross-cutting, overlapping (entities may appear in multiple)</p>`;
    barWrap.appendChild(barHeader);
    wrapper.appendChild(barWrap);

    renderHorizontalBars(barWrap, relational, catData.value, catData.color);
  }
}

function renderPie(container, slices, total, onClick) {
  const width = container.clientWidth || 700;
  const size = Math.min(width, 520);
  const radius = size / 2 * 0.72;
  const innerRadius = radius * 0.48;

  const svg = d3.select(container)
    .append('svg')
    .attr('width', width)
    .attr('height', size)
    .append('g')
    .attr('transform', `translate(${width / 2},${size / 2})`);

  const tooltip = d3.select(container)
    .append('div')
    .attr('class', 'chart-tooltip')
    .style('opacity', 0);

  const pie = d3.pie().value(d => d.value).sort(null).padAngle(0.015);

  const arc = d3.arc().innerRadius(innerRadius).outerRadius(radius).cornerRadius(2);
  const arcHover = d3.arc().innerRadius(innerRadius).outerRadius(radius + 6).cornerRadius(2);
  const labelArc = d3.arc().innerRadius(radius + 14).outerRadius(radius + 14);

  const arcs = svg.selectAll('g.slice')
    .data(pie(slices))
    .join('g')
    .attr('class', 'slice');

  arcs.append('path')
    .attr('d', arc)
    .attr('fill', d => d.data.color)
    .attr('stroke', '#f5f1eb')
    .attr('stroke-width', 2)
    .style('cursor', onClick ? 'pointer' : 'default')
    .style('opacity', 0)
    .transition('enter').duration(600).delay((d, i) => i * 40)
    .style('opacity', 1);

  // Event handlers (use named transition to avoid interrupting entrance)
  arcs.selectAll('path')
    .on('mouseover', function (event, d) {
      d3.select(this).transition('hover').duration(120).attr('d', arcHover);
      const pct = (d.data.value / total * 100).toFixed(1);
      tooltip.transition().duration(120).style('opacity', 1);
      tooltip.html(`<strong>${d.data.name}</strong>${formatCount(d.data.value)} entities (${pct}%)`)
        .style('left', (event.offsetX + 12) + 'px')
        .style('top', (event.offsetY - 12) + 'px');
    })
    .on('mouseout', function () {
      d3.select(this).transition('hover').duration(120).attr('d', arc);
      tooltip.transition().duration(120).style('opacity', 0);
    })
    .on('click', (event, d) => { if (onClick) onClick(d); });

  // Labels
  arcs.append('text')
    .attr('transform', d => {
      const angle = d.endAngle - d.startAngle;
      if (angle < 0.18) return 'translate(9999,9999)';
      return `translate(${labelArc.centroid(d)})`;
    })
    .attr('text-anchor', d => {
      const mid = (d.startAngle + d.endAngle) / 2;
      return mid < Math.PI ? 'start' : 'end';
    })
    .attr('dy', '0.35em')
    .style('font-size', '13px')
    .style('font-weight', '500')
    .style('font-family', "'DM Sans', sans-serif")
    .style('fill', '#6b6560')
    .style('pointer-events', 'none')
    .text(d => {
      const angle = d.endAngle - d.startAngle;
      if (angle < 0.18) return '';
      const pct = (d.data.value / total * 100).toFixed(1);
      const name = d.data.name.length > 18 ? d.data.name.slice(0, 16) + '..' : d.data.name;
      return `${name} ${pct}%`;
    });

  // Center
  const centerGroup = svg.append('g')
    .style('cursor', onClick ? 'default' : 'pointer')
    .on('click', () => { if (!onClick) renderPieCategories(); });

  centerGroup.append('circle')
    .attr('r', innerRadius)
    .attr('fill', 'transparent');

  centerGroup.append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', '-0.2em')
    .style('font-size', '28px')
    .style('font-family', "'Instrument Serif', serif")
    .style('fill', '#1a1a1a')
    .text(formatCount(total));

  centerGroup.append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', '1.4em')
    .style('font-size', '13px')
    .style('fill', '#a09a93')
    .style('font-family', "'DM Sans', sans-serif")
    .text(onClick ? 'entities' : 'back');
}

// ============================================
// HORIZONTAL BAR CHART
// ============================================
function renderHorizontalBars(container, items, total, baseColor) {
  const margin = { top: 4, right: 60, bottom: 4, left: 110 };
  const barHeight = 26;
  const gap = 4;
  const width = Math.min(container.clientWidth || 500, 500);
  const height = items.length * (barHeight + gap) + margin.top + margin.bottom;

  const svg = d3.select(container)
    .append('svg')
    .attr('width', width)
    .attr('height', height);

  const maxVal = d3.max(items, d => d.value);

  const x = d3.scaleLinear()
    .domain([0, maxVal])
    .range([0, width - margin.left - margin.right]);

  const g = svg.append('g')
    .attr('transform', `translate(${margin.left},${margin.top})`);

  const tooltip = d3.select(container)
    .append('div')
    .attr('class', 'chart-tooltip')
    .style('opacity', 0);

  const barColor = d3.color(baseColor) || d3.color('#7a9eb8');

  items.forEach((d, i) => {
    const y = i * (barHeight + gap);
    const pct = (d.value / total * 100).toFixed(1);

    // Bar
    g.append('rect')
      .attr('x', 0)
      .attr('y', y)
      .attr('width', 0)
      .attr('height', barHeight)
      .attr('fill', barColor.brighter(0.3))
      .attr('rx', 3)
      .attr('opacity', 0.8)
      .on('mouseover', function (event) {
        d3.select(this).attr('opacity', 1);
        tooltip.transition().duration(120).style('opacity', 1);
        tooltip.html(`<strong>${d.name}</strong>${d.value.toLocaleString()} entities (${pct}%)`)
          .style('left', (event.offsetX + 10) + 'px')
          .style('top', (event.offsetY - 10) + 'px');
      })
      .on('mouseout', function () {
        d3.select(this).attr('opacity', 0.8);
        tooltip.transition().duration(120).style('opacity', 0);
      })
      .transition().duration(500).delay(i * 30)
      .attr('width', x(d.value));

    // Label (left)
    g.append('text')
      .attr('x', -6)
      .attr('y', y + barHeight / 2)
      .attr('text-anchor', 'end')
      .attr('dy', '0.35em')
      .style('font-size', '12px')
      .style('font-family', "'DM Sans', sans-serif")
      .style('fill', '#6b6560')
      .text(d.name);

    // Count + pct (right)
    g.append('text')
      .attr('x', x(d.value) + 6)
      .attr('y', y + barHeight / 2)
      .attr('dy', '0.35em')
      .style('font-size', '11px')
      .style('font-family', "'JetBrains Mono', monospace")
      .style('fill', '#a09a93')
      .text(`${formatCountShort(d.value)}  ${pct}%`)
      .style('opacity', 0)
      .transition().duration(500).delay(i * 30)
      .style('opacity', 1);
  });
}

// ============================================
// (Treemap removed — replaced by bar charts in Distribution and File views)
// ============================================

// ============================================
// SCHEMA CHORD DIAGRAM
// ============================================
async function loadSchemaData() {
  try {
    const response = await fetch('wikigraph_schema.csv');
    if (!response.ok) throw new Error('Failed to load wikigraph_schema.csv');
    const text = await response.text();
    const rows = text.trim().split('\n').slice(1).map(line => {
      const [source, rel_type, target] = line.split(',');
      return { source, rel_type, target };
    });
    schemaData = rows;

    // Populate filter dropdown
    const modules = [...new Set(rows.map(r => r.rel_type))].sort();
    modules.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m.toLowerCase();
      schemaFilterEl.appendChild(opt);
    });
  } catch (error) {
    console.error('Schema data not available:', error);
    schemaData = null;
  }
}

function renderSchema() {
  if (!schemaData) {
    schemaChordEl.innerHTML = '<div class="loading-state">Schema data not loaded.</div>';
    return;
  }

  const filterMod = schemaFilterEl.value;
  const rows = filterMod === 'all' ? schemaData : schemaData.filter(r => r.rel_type === filterMod);

  // Map category names to TAG_FILES order/colors
  const catNameMap = {
    'PERSON': { label: 'People', color: '#e06c5a', idx: 0 },
    'ORGANIZATION': { label: 'Organizations', color: '#5b9bd5', idx: 1 },
    'PLACE': { label: 'Places', color: '#4ec89e', idx: 2 },
    'EVENT': { label: 'Events', color: '#a87bce', idx: 3 },
    'WORK': { label: 'Works', color: '#e0974a', idx: 4 },
    'PRODUCT': { label: 'Products', color: '#45bfb0', idx: 5 },
    'SCIENCE': { label: 'Science', color: '#7a9eb8', idx: 6 },
    'KNOWLEDGE': { label: 'Knowledge', color: '#b8782d', idx: 7 },
  };

  const catKeys = Object.keys(catNameMap);
  const n = catKeys.length;

  // Build matrix: count of distinct rel_types between each pair
  const matrix = Array.from({ length: n }, () => Array(n).fill(0));
  // Track which modules connect each pair
  const pairModules = Array.from({ length: n }, () => Array.from({ length: n }, () => new Set()));

  rows.forEach(r => {
    const si = catNameMap[r.source]?.idx;
    const ti = catNameMap[r.target]?.idx;
    if (si !== undefined && ti !== undefined) {
      matrix[si][ti]++;
      pairModules[si][ti].add(r.rel_type);
    }
  });

  renderChordDiagram(matrix, catKeys, catNameMap, pairModules, rows);
  renderSchemaDetailPanel(catKeys, catNameMap, pairModules, rows);
  schemaChart = true;
}

function renderChordDiagram(matrix, catKeys, catNameMap, pairModules, rows) {
  const container = schemaChordEl;
  container.innerHTML = '';

  const containerW = container.clientWidth || 700;
  const size = Math.min(containerW, 620);
  const outerRadius = size / 2 - 90;
  const innerRadius = outerRadius - 22;
  const n = catKeys.length;

  const svg = d3.select(container)
    .append('svg')
    .attr('width', size)
    .attr('height', size)
    .append('g')
    .attr('transform', `translate(${size / 2},${size / 2})`);

  const tooltip = d3.select(container)
    .append('div')
    .attr('class', 'chart-tooltip')
    .style('opacity', 0);

  // Build chord layout
  const chord = d3.chord()
    .padAngle(0.06)
    .sortSubgroups(d3.descending)
    .sortChords(d3.descending);

  const chords = chord(matrix);

  const arc = d3.arc().innerRadius(innerRadius).outerRadius(outerRadius);
  const ribbon = d3.ribbon().radius(innerRadius - 1);

  // ── Draw outer arcs (category segments) ──
  const groupG = svg.append('g').attr('class', 'groups');

  const groups = groupG.selectAll('g')
    .data(chords.groups)
    .join('g')
    .style('cursor', 'pointer');

  groups.append('path')
    .attr('d', arc)
    .attr('fill', d => catNameMap[catKeys[d.index]].color)
    .attr('stroke', '#f5f1eb')
    .attr('stroke-width', 2)
    .style('opacity', 0)
    .transition('enter').duration(600).delay((d, i) => i * 60)
    .style('opacity', 0.9);

  // Category labels on arc
  groups.append('text')
    .each(d => { d.angle = (d.startAngle + d.endAngle) / 2; })
    .attr('dy', '0.35em')
    .attr('transform', d => {
      const angle = d.angle * 180 / Math.PI - 90;
      const flip = d.angle > Math.PI;
      return `rotate(${angle}) translate(${outerRadius + 14}) ${flip ? 'rotate(180)' : ''}`;
    })
    .attr('text-anchor', d => d.angle > Math.PI ? 'end' : 'start')
    .text(d => catNameMap[catKeys[d.index]].label)
    .style('font-family', "'Instrument Serif', serif")
    .style('font-size', '13px')
    .style('fill', '#1a1a1a')
    .style('pointer-events', 'none')
    .style('opacity', 0)
    .transition('enter').duration(400).delay((d, i) => 400 + i * 60)
    .style('opacity', 1);

  // Connection count under label
  groups.append('text')
    .each(d => { d.angle = (d.startAngle + d.endAngle) / 2; })
    .attr('dy', '1.8em')
    .attr('transform', d => {
      const angle = d.angle * 180 / Math.PI - 90;
      const flip = d.angle > Math.PI;
      return `rotate(${angle}) translate(${outerRadius + 14}) ${flip ? 'rotate(180)' : ''}`;
    })
    .attr('text-anchor', d => d.angle > Math.PI ? 'end' : 'start')
    .text(d => {
      const total = d3.sum(matrix[d.index]) + d3.sum(matrix.map(row => row[d.index]));
      return `${total} connections`;
    })
    .style('font-family', "'JetBrains Mono', monospace")
    .style('font-size', '9px')
    .style('fill', '#a09a93')
    .style('pointer-events', 'none')
    .style('opacity', 0)
    .transition('enter').duration(400).delay((d, i) => 500 + i * 60)
    .style('opacity', 1);

  // ── Draw ribbons (chords) ──
  const ribbonG = svg.append('g').attr('class', 'ribbons');

  const ribbons = ribbonG.selectAll('path')
    .data(chords)
    .join('path')
    .attr('d', ribbon)
    .attr('fill', d => catNameMap[catKeys[d.source.index]].color)
    .attr('stroke', 'none')
    .style('opacity', 0)
    .style('mix-blend-mode', 'multiply');

  ribbons.transition('enter').duration(800).delay(600)
    .style('opacity', 0.35);

  // ── Hover interactions ──
  let activeIdx = null;

  function highlightCategory(idx) {
    activeIdx = idx;
    groups.selectAll('path')
      .transition('hl').duration(120)
      .style('opacity', d => d.index === idx ? 1 : 0.15);
    ribbons
      .transition('hl').duration(120)
      .style('opacity', d =>
        d.source.index === idx || d.target.index === idx ? 0.6 : 0.03
      );
  }

  function highlightChord(d) {
    activeIdx = null;
    groups.selectAll('path')
      .transition('hl').duration(120)
      .style('opacity', g =>
        g.index === d.source.index || g.index === d.target.index ? 1 : 0.15
      );
    ribbons
      .transition('hl').duration(120)
      .style('opacity', r =>
        r.source.index === d.source.index && r.target.index === d.target.index ? 0.7 : 0.03
      );
  }

  function resetHighlight() {
    activeIdx = null;
    groups.selectAll('path')
      .transition('hl').duration(200)
      .style('opacity', 0.9);
    ribbons
      .transition('hl').duration(200)
      .style('opacity', 0.35);
    tooltip.transition().duration(120).style('opacity', 0);
  }

  groups
    .on('mouseover', function (event, d) {
      highlightCategory(d.index);
      const cat = catNameMap[catKeys[d.index]];
      const outgoing = d3.sum(matrix[d.index]);
      const incoming = d3.sum(matrix.map(row => row[d.index]));
      // Unique modules this category participates in
      const mods = new Set();
      pairModules[d.index].forEach(s => s.forEach(m => mods.add(m)));
      pairModules.forEach(row => row[d.index].forEach(m => mods.add(m)));
      tooltip.transition().duration(120).style('opacity', 1);
      tooltip.html(
        `<strong>${cat.label}</strong>` +
        `${outgoing} outgoing, ${incoming} incoming<br/>` +
        `${mods.size} module types`
      )
        .style('left', (event.offsetX + 14) + 'px')
        .style('top', (event.offsetY - 14) + 'px');
    })
    .on('mousemove', function (event) {
      tooltip.style('left', (event.offsetX + 14) + 'px').style('top', (event.offsetY - 14) + 'px');
    })
    .on('mouseout', resetHighlight)
    .on('click', function (event, d) {
      const catKey = catKeys[d.index];
      updateSchemaDetailForCategory(catKey, catKeys, catNameMap, pairModules, rows);
    });

  ribbons
    .on('mouseover', function (event, d) {
      highlightChord(d);
      const src = catNameMap[catKeys[d.source.index]];
      const tgt = catNameMap[catKeys[d.target.index]];
      const mods = pairModules[d.source.index][d.target.index];
      const revMods = pairModules[d.target.index][d.source.index];
      let html = `<strong>${src.label} \u2194 ${tgt.label}</strong>`;
      if (mods.size > 0) html += `${src.label} \u2192: ${[...mods].map(m => m.toLowerCase()).join(', ')}<br/>`;
      if (revMods.size > 0) html += `${tgt.label} \u2192: ${[...revMods].map(m => m.toLowerCase()).join(', ')}`;
      tooltip.transition().duration(120).style('opacity', 1);
      tooltip.html(html)
        .style('left', (event.offsetX + 14) + 'px')
        .style('top', (event.offsetY - 14) + 'px');
    })
    .on('mousemove', function (event) {
      tooltip.style('left', (event.offsetX + 14) + 'px').style('top', (event.offsetY - 14) + 'px');
    })
    .on('mouseout', resetHighlight);

}

function buildPairData(catKeys, catNameMap, pairModules) {
  const pairData = [];
  for (let i = 0; i < catKeys.length; i++) {
    for (let j = 0; j < catKeys.length; j++) {
      if (pairModules[i][j].size > 0) {
        pairData.push({
          source: catKeys[i],
          target: catKeys[j],
          modules: [...pairModules[i][j]],
          count: pairModules[i][j].size
        });
      }
    }
  }
  pairData.sort((a, b) => b.count - a.count);
  return pairData;
}

function renderPairList(pairData, catNameMap, limit) {
  const pairsEl = document.getElementById('schemaPairs');
  pairsEl.innerHTML = '';
  pairData.slice(0, limit).forEach(p => {
    const src = catNameMap[p.source];
    const tgt = catNameMap[p.target];
    const div = document.createElement('div');
    div.className = 'schema-pair';
    div.innerHTML = `
      <div class="schema-pair-header">
        <span class="schema-pair-route">
          <span style="color:${src.color}">${src.label}</span>
          <span class="schema-pair-arrow">\u2192</span>
          <span style="color:${tgt.color}">${tgt.label}</span>
        </span>
        <span class="schema-pair-count">${p.count}</span>
      </div>
      <div class="schema-modules">
        ${p.modules.map(m => `<span class="schema-mod-chip">${m.toLowerCase()}</span>`).join('')}
      </div>
    `;
    pairsEl.appendChild(div);
  });
}

function renderSchemaDetailPanel(catKeys, catNameMap, pairModules, rows) {
  const panel = schemaDetailEl;
  const uniqueModules = [...new Set(rows.map(r => r.rel_type))];
  const selfLoops = rows.filter(r => r.source === r.target).length;
  const crossCat = rows.length - selfLoops;

  panel.innerHTML = `
    <div class="schema-detail-title">Connection Summary</div>
    <div class="schema-detail-sub">${rows.length} total edges</div>
    <div class="schema-summary-grid">
      <div class="schema-stat">
        <div class="label">Modules</div>
        <div class="value">${uniqueModules.length}</div>
      </div>
      <div class="schema-stat">
        <div class="label">Cross-cat</div>
        <div class="value">${crossCat}</div>
      </div>
      <div class="schema-stat">
        <div class="label">Self-loops</div>
        <div class="value">${selfLoops}</div>
      </div>
      <div class="schema-stat">
        <div class="label">Categories</div>
        <div class="value">${catKeys.length}</div>
      </div>
    </div>
    <div class="schema-detail-title" style="font-size:0.95rem; margin-top:0.5rem">Top Category Pairs</div>
    <div id="schemaPairs"></div>
  `;

  const pairData = buildPairData(catKeys, catNameMap, pairModules);
  renderPairList(pairData, catNameMap, 20);
}

function updateSchemaDetailForCategory(catKey, catKeys, catNameMap, pairModules, rows) {
  const panel = schemaDetailEl;
  const cat = catNameMap[catKey];
  const catRows = rows.filter(r => r.source === catKey || r.target === catKey);
  const uniqueModules = [...new Set(catRows.map(r => r.rel_type))];
  const outgoing = rows.filter(r => r.source === catKey).length;
  const incoming = rows.filter(r => r.target === catKey).length;
  const selfLoops = rows.filter(r => r.source === catKey && r.target === catKey).length;

  panel.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.15rem">
      <div class="schema-detail-title" style="color:${cat.color}">${cat.label}</div>
      <button id="schemaDetailBack" class="btn-ghost" style="font-size:0.8rem; padding:0.2rem 0.5rem">All</button>
    </div>
    <div class="schema-detail-sub">${catRows.length} edges involving this category</div>
    <div class="schema-summary-grid">
      <div class="schema-stat">
        <div class="label">Outgoing</div>
        <div class="value">${outgoing}</div>
      </div>
      <div class="schema-stat">
        <div class="label">Incoming</div>
        <div class="value">${incoming}</div>
      </div>
      <div class="schema-stat">
        <div class="label">Self-loops</div>
        <div class="value">${selfLoops}</div>
      </div>
      <div class="schema-stat">
        <div class="label">Modules</div>
        <div class="value">${uniqueModules.length}</div>
      </div>
    </div>
    <div class="schema-detail-title" style="font-size:0.95rem; margin-top:0.5rem">${cat.label} Pairs</div>
    <div id="schemaPairs"></div>
  `;

  // Filter pairs to only those involving this category
  const allPairs = buildPairData(catKeys, catNameMap, pairModules);
  const filtered = allPairs.filter(p => p.source === catKey || p.target === catKey);
  renderPairList(filtered, catNameMap, filtered.length);

  // Back button restores full view
  document.getElementById('schemaDetailBack').addEventListener('click', () => {
    renderSchemaDetailPanel(catKeys, catNameMap, pairModules, rows);
  });
}

// ============================================
// FORCE-DIRECTED GRAPH
// ============================================
function renderGraph() {
  const container = graphContentEl;
  container.innerHTML = '';

  const width = container.clientWidth || 900;
  const height = container.clientHeight || 650;

  const { nodes, links } = buildGraphData();

  const svg = d3.select(container)
    .append('svg')
    .attr('width', width)
    .attr('height', height)
    .attr('viewBox', [0, 0, width, height]);

  const tooltip = d3.select(container)
    .append('div')
    .attr('class', 'chart-tooltip')
    .style('opacity', 0);

  // Glow filter for category nodes
  const defs = svg.append('defs');
  const glowFilter = defs.append('filter').attr('id', 'glow');
  glowFilter.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'coloredBlur');
  const feMerge = glowFilter.append('feMerge');
  feMerge.append('feMergeNode').attr('in', 'coloredBlur');
  feMerge.append('feMergeNode').attr('in', 'SourceGraphic');

  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).distance(100))
    .force('charge', d3.forceManyBody().strength(-300))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(d => d.radius + 8));

  const link = svg.append('g')
    .selectAll('line')
    .data(links)
    .join('line')
    .attr('stroke', '#e8e3dc')
    .attr('stroke-width', 1);

  const node = svg.append('g')
    .selectAll('g')
    .data(nodes)
    .join('g')
    .call(drag(simulation));

  node.append('circle')
    .attr('r', d => d.radius)
    .attr('fill', d => d.color)
    .attr('stroke', d => d.type === 'module' && d.categories?.length > 1 ? '#b8782d' : '#f5f1eb')
    .attr('stroke-width', d => d.type === 'module' && d.categories?.length > 1 ? 2.5 : 1.5)
    .attr('opacity', d => d.type === 'category' ? 0.9 : 0.7)
    .style('filter', d => d.type === 'category' ? 'url(#glow)' : 'none')
    .style('cursor', 'pointer')
    .on('mouseover', function (event, d) {
      d3.select(this).attr('opacity', 1);
      tooltip.transition().duration(120).style('opacity', 1);
      let content = `<strong>${d.name}</strong>${d.type}`;
      if (d.type === 'module') {
        const isShared = d.categories.length > 1;
        content += `<br/>${isShared ? 'Shared: ' : ''}${d.categories.join(', ')}`;
        content += `<br/>Indicators: ${d.indicators} | Props: ${d.valueProps}`;
      }
      tooltip.html(content)
        .style('left', (event.offsetX + 12) + 'px')
        .style('top', (event.offsetY - 12) + 'px');
    })
    .on('mouseout', function (event, d) {
      d3.select(this).attr('opacity', d.type === 'category' ? 0.9 : 0.7);
      tooltip.transition().duration(120).style('opacity', 0);
    });

  node.append('text')
    .text(d => d.name.length > 14 ? d.name.slice(0, 12) + '..' : d.name)
    .attr('x', d => d.radius + 5)
    .attr('y', 4)
    .style('font-size', d => d.type === 'category' ? '11px' : '9px')
    .style('font-weight', d => d.type === 'category' ? '600' : '400')
    .style('font-family', d => d.type === 'category' ? "'Instrument Serif', serif" : "'DM Sans', sans-serif")
    .style('fill', d => d.type === 'category' ? '#1a1a1a' : '#6b6560')
    .style('pointer-events', 'none');

  simulation.on('tick', () => {
    link
      .attr('x1', d => d.source.x)
      .attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x)
      .attr('y2', d => d.target.y);

    node.attr('transform', d => {
      d.x = Math.max(d.radius, Math.min(width - d.radius, d.x));
      d.y = Math.max(d.radius, Math.min(height - d.radius, d.y));
      return `translate(${d.x},${d.y})`;
    });
  });

  function drag(simulation) {
    return d3.drag()
      .on('start', (event) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        event.subject.fx = event.subject.x;
        event.subject.fy = event.subject.y;
      })
      .on('drag', (event) => {
        event.subject.fx = event.x;
        event.subject.fy = event.y;
      })
      .on('end', (event) => {
        if (!event.active) simulation.alphaTarget(0);
        event.subject.fx = null;
        event.subject.fy = null;
      });
  }

  graphChart = true;
}


// ============================================
// DATA BUILDERS
// ============================================
function buildGraphData() {
  const entries = Object.values(allData).filter(e => e.data);
  const nodeMap = new Map();
  const links = [];
  const cleanModuleName = (name) => {
    if (!name) return '';
    const normalized = typeof name.normalize === 'function' ? name.normalize('NFKC') : name;
    return normalized.replace(/\s+/g, ' ').trim();
  };
  const normalizeModuleKey = (name) => cleanModuleName(name).toLowerCase();
  const sharedModuleColor = '#b8782d';

  entries.forEach(entry => {
    const catNode = {
      id: `cat:${entry.label}`,
      name: entry.label,
      type: 'category',
      color: entry.color,
      radius: 18
    };
    nodeMap.set(catNode.id, catNode);
  });

  entries.forEach(entry => {
    const catId = `cat:${entry.label}`;
    const modules = entry.data?.modules || {};

    Object.entries(modules).forEach(([modName, mod]) => {
      const moduleKey = normalizeModuleKey(modName);
      const modId = `mod:${moduleKey}`;
      const displayName = cleanModuleName(modName);
      let modNode = nodeMap.get(modId);

      if (!modNode) {
        const indicators = mod?.indicators ? Object.keys(mod.indicators).length : 0;
        const valueProps = mod?.value_props?.length || 0;
        modNode = {
          id: modId,
          name: displayName,
          type: 'module',
          categories: [entry.label],
          colors: [entry.color],
          color: d3.color(entry.color).brighter(0.4),
          radius: 8,
          indicators,
          valueProps
        };
        nodeMap.set(modId, modNode);
      } else {
        if (!modNode.categories.includes(entry.label)) modNode.categories.push(entry.label);
        if (!modNode.colors.includes(entry.color)) modNode.colors.push(entry.color);
        modNode.radius = 12 + modNode.categories.length * 3;
        modNode.color = sharedModuleColor;
      }

      links.push({ source: catId, target: modId });
    });
  });

  const nodes = Array.from(nodeMap.values());
  const resolvedLinks = links.map(link => ({
    source: nodeMap.get(link.source),
    target: nodeMap.get(link.target)
  }));

  return { nodes, links: resolvedLinks };
}

// ============================================
// OVERVIEW
// ============================================
function renderOverview() {
  const entries = Object.values(allData).filter(e => e.data);

  let totalModules = 0;
  let totalGateQids = 0;

  const categoryStats = entries.map(entry => {
    const stats = calculateStats(entry.data);
    totalModules += stats.moduleCount;
    totalGateQids += stats.gateCount;
    return { ...entry, stats };
  });

  // Sort by entity count
  categoryStats.sort((a, b) => {
    const catKeyA = a.file.replace('.yaml', '');
    const catKeyB = b.file.replace('.yaml', '');
    const countA = entityCounts?.categories?.[catKeyA]?.count || 0;
    const countB = entityCounts?.categories?.[catKeyB]?.count || 0;
    return countB - countA;
  });

  const maxCount = entityCounts ? Math.max(...Object.values(entityCounts.categories).map(c => c.count)) : 1;

  overviewContentEl.innerHTML = `
    <div class="hero-stats">
      <div class="hero-stat accent">
        <div class="label">Total Entities</div>
        <div class="value">${entityCounts ? formatCount(entityCounts.total) : '\u2014'}</div>
      </div>
      <div class="hero-stat">
        <div class="label">Classified</div>
        <div class="value">${entityCounts ? formatCount(entityCounts.classified) : '\u2014'} <span class="unit">${entityCounts ? '(' + (entityCounts.classified / entityCounts.total * 100).toFixed(1) + '%)' : ''}</span></div>
      </div>
      <div class="hero-stat">
        <div class="label">Modules</div>
        <div class="value">${totalModules}</div>
      </div>
    </div>

    <div class="cat-grid">
      ${categoryStats.map(cat => {
        const catKey = cat.file.replace('.yaml', '');
        const catCounts = entityCounts?.categories?.[catKey];
        const count = catCounts?.count || 0;
        const noModule = catCounts?.no_module || 0;
        const pct = count > 0 ? (count / maxCount * 100).toFixed(0) : 0;
        const topModules = cat.stats.modules.slice(0, 6);
        const moreCount = cat.stats.modules.length - 6;

        return `
          <div class="cat-card" style="--cat-color: ${cat.color}" data-file="${cat.file}">
            <div class="cat-card-head">
              <span class="cat-card-name">${cat.label}</span>
              <span class="cat-card-count">${count > 0 ? formatCountShort(count) : cat.stats.moduleCount + ' modules'}</span>
            </div>
            <div class="cat-card-bar">
              <div class="cat-card-bar-fill" style="width: ${pct}%; background: ${cat.color}"></div>
            </div>
            <div class="cat-card-modules">
              ${topModules.map(m => `<span class="mod-chip">${m}</span>`).join('')}
              ${moreCount > 0 ? `<span class="mod-chip more">+${moreCount}</span>` : ''}
            </div>
          </div>
        `;
      }).join('')}
    </div>

    <h3 class="section-heading">Category Breakdown</h3>
    <div class="comparison-table-wrapper">
      <table class="comparison-table">
        <thead>
          <tr>
            <th>Category</th>
            <th>Entities</th>
            <th>No Module</th>
            <th>Gates</th>
            <th>Modules</th>
            <th>Indicators</th>
          </tr>
        </thead>
        <tbody>
          ${categoryStats.map(cat => {
            const catKey = cat.file.replace('.yaml', '');
            const catCounts = entityCounts?.categories?.[catKey];
            return `
            <tr class="clickable-row" data-file="${cat.file}">
              <td>
                <span class="color-dot" style="background: ${cat.color}"></span>
                ${cat.label}
              </td>
              <td>${catCounts ? catCounts.count.toLocaleString() : '\u2014'}</td>
              <td>${catCounts ? catCounts.no_module.toLocaleString() : '\u2014'}</td>
              <td>${cat.stats.gateCount}</td>
              <td>${cat.stats.moduleCount}</td>
              <td>${cat.stats.indicatorKeys}</td>
            </tr>
          `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Click handlers
  document.querySelectorAll('.cat-card').forEach(card => {
    card.addEventListener('click', () => loadFile(card.dataset.file));
  });

  document.querySelectorAll('.clickable-row').forEach(row => {
    row.addEventListener('click', () => loadFile(row.dataset.file));
  });
}

// ============================================
// TAXONOMY BIPARTITE GRAPH
// ============================================
function renderHierarchy() {
  const container = hierarchyContentEl;
  container.innerHTML = '';

  const entries = Object.values(allData).filter(e => e.data);
  if (!entries.length) {
    container.innerHTML = '<div class="loading-state">No data loaded.</div>';
    return;
  }

  // ── Build taxonomy data ──
  const normalizeKey = name => (name || '').normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase();

  const moduleMap = new Map();
  entries.forEach(entry => {
    const catKey = entry.file.replace('.yaml', '');
    Object.entries(entry.data?.modules || {}).forEach(([name]) => {
      const key = normalizeKey(name);
      if (!moduleMap.has(key)) moduleMap.set(key, { name: name.trim(), categories: [] });
      const m = moduleMap.get(key);
      if (!m.categories.includes(catKey)) m.categories.push(catKey);
    });
  });

  const categories = entries.map(entry => {
    const key = entry.file.replace('.yaml', '');
    return {
      key,
      label: entry.label,
      color: entry.color,
      count: entityCounts?.categories?.[key]?.count || 0,
      moduleCount: Object.keys(entry.data?.modules || {}).length
    };
  }).sort((a, b) => b.count - a.count);

  categories.forEach((c, i) => c.idx = i);
  const catByKey = new Map(categories.map(c => [c.key, c.idx]));

  const relModules = [];
  moduleMap.forEach((mod, key) => {
    if (mod.categories.length >= 2) relModules.push({ key, name: mod.name, categories: mod.categories });
  });
  relModules.sort((a, b) => b.categories.length - a.categories.length);
  relModules.forEach((m, i) => m.idx = i);

  const links = [];
  relModules.forEach(mod => {
    mod.categories.forEach(ck => {
      const ci = catByKey.get(ck);
      if (ci !== undefined) links.push({ ci, mi: mod.idx, color: categories[ci].color });
    });
  });

  // Build intrinsic modules per category
  categories.forEach(c => {
    c.structModules = [];
    const catEntry = entries.find(e => e.file.replace('.yaml', '') === c.key);
    const catModules = catEntry?.data?.modules || {};
    Object.entries(catModules).forEach(([name]) => {
      const key = normalizeKey(name);
      const modInfo = moduleMap.get(key);
      if (modInfo && modInfo.categories.length === 1) {
        const count = entityCounts?.categories?.[c.key]?.modules?.[name] || 0;
        c.structModules.push({ name: name.trim(), count });
      }
    });
    c.structModules.sort((a, b) => b.count - a.count);
  });

  if (relModules.length === 0) {
    container.innerHTML = '<div class="loading-state">No relational modules found.</div>';
    return;
  }

  // ── Layout ──
  const containerW = container.clientWidth || 900;
  const catPillH = 38;
  const modPillH = 30;
  const catFontSize = 13.5;
  const modFontSize = 12;
  const padX = 18;
  const catGap = 14;
  const modGap = 10;

  // Measure text widths
  const tempSvg = d3.select(container).append('svg')
    .style('position', 'absolute').style('visibility', 'hidden').style('pointer-events', 'none');

  function measureText(text, size, family) {
    const t = tempSvg.append('text').style('font-size', size + 'px').style('font-family', family).text(text);
    const w = t.node().getComputedTextLength();
    t.remove();
    return w;
  }

  categories.forEach(c => {
    c.textW = measureText(c.label, catFontSize, "'Instrument Serif', serif");
    c.pillW = c.textW + padX * 2;
  });

  relModules.forEach(m => {
    m.textW = measureText(m.name, modFontSize, "'DM Sans', sans-serif");
    m.pillW = m.textW + padX * 2;
  });

  tempSvg.remove();

  // Decide single or double row for modules
  const modTotalWSingle = relModules.reduce((s, m) => s + m.pillW, 0) + (relModules.length - 1) * modGap;
  const useDoubleRow = modTotalWSingle > containerW - 80;

  const catTotalW = categories.reduce((s, c) => s + c.pillW, 0) + (categories.length - 1) * catGap;

  let modRow1, modRow2, modRow1TotalW, modRow2TotalW;
  if (useDoubleRow) {
    const half = Math.ceil(relModules.length / 2);
    modRow1 = relModules.slice(0, half);
    modRow2 = relModules.slice(half);
    modRow1TotalW = modRow1.reduce((s, m) => s + m.pillW, 0) + (modRow1.length - 1) * modGap;
    modRow2TotalW = modRow2.reduce((s, m) => s + m.pillW, 0) + (modRow2.length - 1) * modGap;
  } else {
    modRow1 = relModules;
    modRow2 = [];
    modRow1TotalW = modTotalWSingle;
    modRow2TotalW = 0;
  }

  const contentW = Math.max(catTotalW, modRow1TotalW, modRow2TotalW);
  const margin = 40;
  const svgW = Math.max(contentW + margin * 2, containerW);

  const topY = 50;
  const rowGap = 220;
  const modRow1Y = topY + catPillH + rowGap;
  const modRow2Y = useDoubleRow ? modRow1Y + modPillH + 20 : 0;
  const svgH = (useDoubleRow ? modRow2Y + modPillH : modRow1Y + modPillH) + 50;

  // Position categories
  let cx = (svgW - catTotalW) / 2;
  categories.forEach(c => {
    c.x = cx + c.pillW / 2;
    c.y = topY + catPillH / 2;
    cx += c.pillW + catGap;
  });

  // Position module rows
  let mx1 = (svgW - modRow1TotalW) / 2;
  modRow1.forEach(m => {
    m.x = mx1 + m.pillW / 2;
    m.y = modRow1Y + modPillH / 2;
    mx1 += m.pillW + modGap;
  });

  if (useDoubleRow) {
    let mx2 = (svgW - modRow2TotalW) / 2;
    modRow2.forEach(m => {
      m.x = mx2 + m.pillW / 2;
      m.y = modRow2Y + modPillH / 2;
      mx2 += m.pillW + modGap;
    });
  }

  // ── Render SVG ──
  const svg = d3.select(container)
    .append('svg')
    .attr('width', svgW)
    .attr('height', svgH);

  const defs = svg.append('defs');
  const structShadow = defs.append('filter').attr('id', 'struct-shadow')
    .attr('x', '-10%').attr('y', '-5%').attr('width', '120%').attr('height', '130%');
  structShadow.append('feDropShadow')
    .attr('dx', 0).attr('dy', 3).attr('stdDeviation', 6)
    .attr('flood-color', 'rgba(0,0,0,0.08)');

  const tooltip = d3.select(container)
    .append('div')
    .attr('class', 'chart-tooltip')
    .style('opacity', 0);

  // Level labels
  svg.append('text')
    .attr('x', svgW / 2).attr('y', topY - 14)
    .attr('text-anchor', 'middle')
    .text('CATEGORIES')
    .style('font-family', "'DM Sans', sans-serif")
    .style('font-size', '10px')
    .style('font-weight', '600')
    .style('letter-spacing', '2px')
    .style('fill', '#a09a93');

  svg.append('text')
    .attr('x', svgW / 2).attr('y', modRow1Y - 14)
    .attr('text-anchor', 'middle')
    .text('RELATIONAL MODULES')
    .style('font-family', "'DM Sans', sans-serif")
    .style('font-size', '10px')
    .style('font-weight', '600')
    .style('letter-spacing', '2px')
    .style('fill', '#a09a93');

  // ── Links ──
  const linkG = svg.append('g');

  const linkPaths = linkG.selectAll('path')
    .data(links)
    .join('path')
    .attr('d', d => {
      const cat = categories[d.ci];
      const mod = relModules[d.mi];
      const y1 = cat.y + catPillH / 2;
      const y2 = mod.y - modPillH / 2;
      return `M ${cat.x} ${y1} L ${mod.x} ${y2}`;
    })
    .attr('fill', 'none')
    .attr('stroke', d => d.color)
    .attr('stroke-width', 1.5)
    .attr('opacity', 0);

  linkPaths.transition('enter').duration(600).delay(500)
    .attr('opacity', 0.18);

  // ── Category nodes ──
  const catG = svg.append('g');

  const catGroups = catG.selectAll('g')
    .data(categories)
    .join('g')
    .attr('transform', d => `translate(${d.x}, ${d.y})`)
    .style('cursor', 'pointer')
    .style('opacity', 0);

  catGroups.transition('enter').duration(400).delay((d, i) => i * 40)
    .style('opacity', 1);

  catGroups.append('rect')
    .attr('x', d => -d.pillW / 2)
    .attr('y', -catPillH / 2)
    .attr('width', d => d.pillW)
    .attr('height', catPillH)
    .attr('rx', catPillH / 2)
    .attr('fill', '#ffffff')
    .attr('stroke', d => d.color)
    .attr('stroke-width', 2);

  catGroups.append('text')
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', 'central')
    .text(d => d.label)
    .style('font-family', "'Instrument Serif', serif")
    .style('font-size', catFontSize + 'px')
    .style('fill', '#1a1a1a')
    .style('pointer-events', 'none');

  // Entity count below category
  catGroups.append('text')
    .attr('text-anchor', 'middle')
    .attr('y', catPillH / 2 + 14)
    .text(d => d.count > 0 ? formatCountShort(d.count) : '')
    .style('font-family', "'JetBrains Mono', monospace")
    .style('font-size', '10px')
    .style('fill', '#a09a93')
    .style('pointer-events', 'none');

  // ── Module nodes ──
  const modG = svg.append('g');

  const modGroups = modG.selectAll('g')
    .data(relModules)
    .join('g')
    .attr('transform', d => `translate(${d.x}, ${d.y})`)
    .style('cursor', 'pointer')
    .style('opacity', 0);

  modGroups.transition('enter').duration(400).delay((d, i) => 300 + i * 25)
    .style('opacity', 1);

  modGroups.append('rect')
    .attr('x', d => -d.pillW / 2)
    .attr('y', -modPillH / 2)
    .attr('width', d => d.pillW)
    .attr('height', modPillH)
    .attr('rx', modPillH / 2)
    .attr('fill', '#faf5ee')
    .attr('stroke', '#b8782d')
    .attr('stroke-width', 1.5);

  modGroups.append('text')
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', 'central')
    .text(d => d.name)
    .style('font-family', "'DM Sans', sans-serif")
    .style('font-size', modFontSize + 'px')
    .style('fill', '#6b6560')
    .style('pointer-events', 'none');

  // Connection count badge
  modGroups.append('circle')
    .attr('cx', d => d.pillW / 2 - 2)
    .attr('cy', -modPillH / 2 + 2)
    .attr('r', 8)
    .attr('fill', '#b8782d');

  modGroups.append('text')
    .attr('x', d => d.pillW / 2 - 2)
    .attr('y', -modPillH / 2 + 2)
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', 'central')
    .text(d => d.categories.length)
    .style('font-family', "'DM Sans', sans-serif")
    .style('font-size', '9px')
    .style('font-weight', '600')
    .style('fill', '#fff')
    .style('pointer-events', 'none');

  // ── Structural module dropdowns (hidden by default) ──
  const structCardW = 180;
  const structRowH = 20;
  const structMaxVisible = 8;
  const structHeaderH = 22;
  const structPadY = 6;
  const structOffsetY = 28;

  const structLayer = svg.append('g').attr('class', 'struct-layer');

  const structDropdowns = structLayer.selectAll('g.struct-dropdown')
    .data(categories)
    .join('g')
    .attr('class', 'struct-dropdown')
    .attr('transform', d => `translate(${d.x}, ${d.y + catPillH / 2 + structOffsetY})`)
    .style('opacity', 0)
    .style('pointer-events', 'none');

  structDropdowns.each(function (d) {
    const g = d3.select(this);
    const mods = d.structModules.slice(0, structMaxVisible);
    if (mods.length === 0) return;
    const overflow = d.structModules.length - structMaxVisible;
    const totalRows = mods.length + (overflow > 0 ? 1 : 0);
    const cardH = structHeaderH + totalRows * structRowH + structPadY * 2;

    // Connector line
    g.append('line')
      .attr('x1', 0).attr('y1', -structOffsetY + catPillH / 2 + 2)
      .attr('x2', 0).attr('y2', 0)
      .attr('stroke', d.color)
      .attr('stroke-width', 1)
      .attr('stroke-dasharray', '3,3')
      .attr('opacity', 0.4);

    // Card background
    g.append('rect')
      .attr('x', -structCardW / 2)
      .attr('y', 0)
      .attr('width', structCardW)
      .attr('height', cardH)
      .attr('rx', 8)
      .attr('fill', '#ffffff')
      .attr('stroke', '#e0dbd4')
      .attr('stroke-width', 1)
      .attr('filter', 'url(#struct-shadow)');

    // Left accent bar
    g.append('rect')
      .attr('x', -structCardW / 2)
      .attr('y', 0)
      .attr('width', 3)
      .attr('height', cardH)
      .attr('rx', 1.5)
      .attr('fill', d.color);

    // Header
    g.append('text')
      .attr('x', -structCardW / 2 + 12)
      .attr('y', structHeaderH / 2 + 2)
      .attr('dominant-baseline', 'central')
      .text('STRUCTURAL')
      .style('font-family', "'DM Sans', sans-serif")
      .style('font-size', '9px')
      .style('font-weight', '600')
      .style('letter-spacing', '1.5px')
      .style('fill', '#a09a93');

    // Module rows
    mods.forEach((mod, i) => {
      const y = structHeaderH + structPadY + i * structRowH + structRowH / 2;

      g.append('text')
        .attr('x', -structCardW / 2 + 12)
        .attr('y', y)
        .attr('dominant-baseline', 'central')
        .text(mod.name.length > 16 ? mod.name.slice(0, 14) + '..' : mod.name)
        .style('font-family', "'DM Sans', sans-serif")
        .style('font-size', '11px')
        .style('fill', '#4a4540');

      if (mod.count > 0) {
        g.append('text')
          .attr('x', structCardW / 2 - 10)
          .attr('y', y)
          .attr('text-anchor', 'end')
          .attr('dominant-baseline', 'central')
          .text(formatCountShort(mod.count))
          .style('font-family', "'JetBrains Mono', monospace")
          .style('font-size', '10px')
          .style('fill', '#a09a93');
      }
    });

    // Overflow
    if (overflow > 0) {
      const y = structHeaderH + structPadY + mods.length * structRowH + structRowH / 2;
      g.append('text')
        .attr('x', -structCardW / 2 + 12)
        .attr('y', y)
        .attr('dominant-baseline', 'central')
        .text(`+${overflow} more`)
        .style('font-family', "'DM Sans', sans-serif")
        .style('font-size', '11px')
        .style('font-style', 'italic')
        .style('fill', '#a09a93');
    }
  });

  // ── Hover interactions ──
  let hoverTimeout = null;
  let activeCatIdx = null;

  function showCategoryHighlight(d, event) {
    activeCatIdx = d.idx;
    const connMods = new Set();
    links.forEach(l => { if (l.ci === d.idx) connMods.add(l.mi); });

    catGroups.transition('highlight').duration(150).style('opacity', c => c.idx === d.idx ? 1 : 0.1);
    modGroups.transition('highlight').duration(150).style('opacity', m => connMods.has(m.idx) ? 1 : 0.1);
    linkPaths.transition('highlight').duration(150)
      .attr('opacity', l => l.ci === d.idx ? 0.55 : 0.02)
      .attr('stroke-width', l => l.ci === d.idx ? 2.5 : 1.5);

    // Show intrinsic dropdown
    structDropdowns.transition('struct').duration(200)
      .style('opacity', dd => dd.idx === d.idx && dd.structModules.length > 0 ? 1 : 0)
      .style('pointer-events', dd => dd.idx === d.idx && dd.structModules.length > 0 ? 'auto' : 'none');

    if (event) {
      tooltip.transition().duration(120).style('opacity', 1);
      const structCount = d.structModules ? d.structModules.length : 0;
      tooltip.html(
        `<strong>${d.label}</strong>` +
        (d.count > 0 ? `${formatCount(d.count)} entities<br/>` : '') +
        `${structCount} intrinsic · ${connMods.size} relational`
      )
        .style('left', (event.offsetX + 12) + 'px')
        .style('top', (event.offsetY - 48) + 'px');
    }
  }

  function resetHighlight() {
    activeCatIdx = null;
    catGroups.transition('highlight').duration(200).style('opacity', 1);
    modGroups.transition('highlight').duration(200).style('opacity', 1);
    linkPaths.transition('highlight').duration(200).attr('opacity', 0.18).attr('stroke-width', 1.5);
    structDropdowns.transition('struct').duration(200)
      .style('opacity', 0).style('pointer-events', 'none');
    tooltip.transition().duration(120).style('opacity', 0);
  }

  function delayedReset() {
    clearTimeout(hoverTimeout);
    hoverTimeout = setTimeout(resetHighlight, 120);
  }

  catGroups
    .on('mouseover', function (event, d) {
      clearTimeout(hoverTimeout);
      showCategoryHighlight(d, event);
    })
    .on('mousemove', function (event) {
      tooltip.style('left', (event.offsetX + 12) + 'px').style('top', (event.offsetY - 12) + 'px');
    })
    .on('mouseout', delayedReset);

  structDropdowns
    .on('mouseover', function (event, d) {
      clearTimeout(hoverTimeout);
      if (activeCatIdx !== d.idx) showCategoryHighlight(d, null);
    })
    .on('mouseout', delayedReset);

  modGroups
    .on('mouseover', function (event, d) {
      clearTimeout(hoverTimeout);
      activeCatIdx = null;
      structDropdowns.transition('struct').duration(150)
        .style('opacity', 0).style('pointer-events', 'none');

      const connCats = new Set();
      links.forEach(l => { if (l.mi === d.idx) connCats.add(l.ci); });

      modGroups.transition('highlight').duration(150).style('opacity', m => m.idx === d.idx ? 1 : 0.1);
      catGroups.transition('highlight').duration(150).style('opacity', c => connCats.has(c.idx) ? 1 : 0.1);
      linkPaths.transition('highlight').duration(150)
        .attr('opacity', l => l.mi === d.idx ? 0.55 : 0.02)
        .attr('stroke-width', l => l.mi === d.idx ? 2.5 : 1.5);

      tooltip.transition().duration(120).style('opacity', 1);
      const catNames = categories.filter(c => connCats.has(c.idx)).map(c => c.label).join(', ');
      tooltip.html(
        `<strong>${d.name}</strong>` +
        `${d.categories.length} categories:<br/>${catNames}`
      )
        .style('left', (event.offsetX + 12) + 'px')
        .style('top', (event.offsetY - 12) + 'px');
    })
    .on('mousemove', function (event) {
      tooltip.style('left', (event.offsetX + 12) + 'px').style('top', (event.offsetY - 12) + 'px');
    })
    .on('mouseout', delayedReset);

  // ── Summary footer ──
  const footer = document.createElement('div');
  footer.className = 'taxonomy-footer';
  footer.innerHTML = `
    <span>${categories.length} categories</span>
    <span class="taxonomy-dot"></span>
    <span>${relModules.length} relational modules</span>
    <span class="taxonomy-dot"></span>
    <span>${links.length} connections</span>
  `;
  container.appendChild(footer);

  hierarchyChart = true;
}

function calculateStats(data) {
  if (!data || typeof data !== 'object') {
    return { gateCount: 0, coreProps: 0, moduleCount: 0, indicatorKeys: 0, valueProps: 0, modules: [] };
  }

  const gateCount = Array.isArray(data.gate_qids) ? data.gate_qids.length : 0;
  const coreProps = Array.isArray(data?.core?.props) ? data.core.props.length : 0;
  const modules = data?.modules && typeof data.modules === 'object' ? Object.keys(data.modules) : [];
  const moduleCount = modules.length;

  let indicatorKeys = 0;
  let valueProps = 0;

  modules.forEach(moduleKey => {
    const module = data.modules[moduleKey];
    if (!module || typeof module !== 'object') return;
    if (module.indicators && typeof module.indicators === 'object') {
      indicatorKeys += Object.keys(module.indicators).length;
    }
    if (Array.isArray(module.value_props)) {
      valueProps += module.value_props.length;
    }
  });

  return { gateCount, coreProps, moduleCount, indicatorKeys, valueProps, modules };
}

// ============================================
// FILE VIEW
// ============================================
function renderFileList(files) {
  fileListEl.innerHTML = '';
  files.forEach(entry => {
    const button = document.createElement('button');
    button.className = 'file-button';
    button.innerHTML = `<span class="color-dot" style="background: ${entry.color}; color: ${entry.color}"></span>${entry.label}`;
    button.dataset.file = entry.file;
    button.addEventListener('click', () => loadFile(entry.file));
    fileListEl.appendChild(button);
  });
  if (currentFile) setActiveButton(currentFile);
}

function handleFilter(event) {
  const query = event.target.value.toLowerCase().trim();
  const filtered = TAG_FILES.filter(entry => {
    return entry.label.toLowerCase().includes(query) || entry.file.toLowerCase().includes(query);
  });
  renderFileList(filtered);
}

async function loadFile(file) {
  switchMainView('file');
  currentFile = file;
  setActiveButton(file);

  // Update nav: deactivate all nav pills when in file view
  document.querySelectorAll('#mainNav .nav-pill').forEach(btn => btn.classList.remove('active'));

  const entry = TAG_FILES.find(e => e.file === file);
  fileTitleEl.textContent = entry ? entry.label : file;
  fileMetaEl.textContent = 'Loading...';

  try {
    const cached = allData[file];
    let data, text;

    if (cached && cached.data) {
      data = cached.data;
      text = cached.text;
    } else {
      const response = await fetch(`../src/tags/${file}`);
      if (!response.ok) throw new Error(`Failed to load ${file}`);
      text = await response.text();
      data = jsyaml.load(text);
    }

    rawCodeEl.innerHTML = escapeHtml(text);
    renderSummary(data);
    renderTree(data);
    renderFileTreemap(file, entry);

    const stats = calculateStats(data);
    const catKey = file.replace('.yaml', '');
    const catCounts = entityCounts?.categories?.[catKey];
    const countStr = catCounts ? ` \u00b7 ${catCounts.count.toLocaleString()} entities` : '';
    fileMetaEl.textContent = `${stats.gateCount} gates \u00b7 ${stats.moduleCount} modules \u00b7 ${stats.indicatorKeys} indicators${countStr}`;
  } catch (error) {
    fileMetaEl.textContent = 'Unable to load file.';
    treeRootEl.innerHTML = `<div class="empty">${escapeHtml(String(error.message || error))}</div>`;
    summaryPanelEl.innerHTML = '';
    rawCodeEl.textContent = '';
  }
}

function setActiveButton(file) {
  document.querySelectorAll('.file-button').forEach(button => {
    button.classList.toggle('active', button.dataset.file === file);
  });
}

function setActivePanel(panel) {
  viewToggleButtons.forEach(button => {
    button.classList.toggle('active', button.dataset.view === panel);
  });

  summaryPanelEl.classList.toggle('hidden', panel !== 'summary');
  treePanelEl.classList.toggle('hidden', panel !== 'tree');
  rawPanelEl.classList.toggle('hidden', panel !== 'raw');
}

function renderSummary(data) {
  if (!data || typeof data !== 'object') {
    summaryPanelEl.innerHTML = '';
    return;
  }

  const stats = calculateStats(data);

  summaryPanelEl.innerHTML = `
    <div class="summary-grid">
      <div class="summary-card"><h3>Gate P31 QIDs</h3><p>${stats.gateCount}</p></div>
      <div class="summary-card"><h3>Core Props</h3><p>${stats.coreProps}</p></div>
      <div class="summary-card"><h3>Modules</h3><p>${stats.moduleCount}</p></div>
      <div class="summary-card"><h3>Indicators</h3><p>${stats.indicatorKeys}</p></div>
      <div class="summary-card"><h3>Value Props</h3><p>${stats.valueProps}</p></div>
    </div>
    ${stats.modules.length > 0 ? `
      <div class="module-list">
        <h3>Modules</h3>
        <div class="chips">${stats.modules.map(m => `<span class="chip">${m}</span>`).join('')}</div>
      </div>
    ` : ''}
  `;
}

function renderFileTreemap(file, entry) {
  const container = document.getElementById('fileTreemap');
  container.innerHTML = '';

  if (!entityCounts || !entry) return;
  const catKey = file.replace('.yaml', '');
  const catData = entityCounts.categories[catKey];
  if (!catData || !catData.modules || Object.keys(catData.modules).length === 0) return;

  const color = entry.color;
  const yamlModules = allData[file]?.data?.modules || {};

  const intrinsic = [];
  const relational = [];

  Object.entries(catData.modules).forEach(([name, count]) => {
    if (count <= 0) return;
    const modType = yamlModules[name]?.type || 'intrinsic';
    const item = { name, value: count };
    if (modType === 'relational') relational.push(item);
    else intrinsic.push(item);
  });

  intrinsic.sort((a, b) => b.value - a.value);
  relational.sort((a, b) => b.value - a.value);

  renderSchemaCard(container, entry, catData, intrinsic, relational, color);
}

function renderSchemaCard(container, entry, catData, intrinsic, relational, color) {
  const cardW = Math.min(container.clientWidth || 480, 480);
  const rowH = 28;
  const headerH = 46;
  const sectionLabelH = 28;
  const barInset = 14;
  const countW = 72;
  const stubLen = 28;
  const barMaxW = cardW - barInset - countW - 8;

  const sRows = intrinsic;
  const rRows = relational;
  const hasBoth = sRows.length > 0 && rRows.length > 0;

  const sBodyH = sRows.length * rowH;
  const rBodyH = rRows.length * rowH;
  const structSectionH = sRows.length > 0 ? sectionLabelH + sBodyH : 0;
  const relSectionH = rRows.length > 0 ? sectionLabelH + rBodyH : 0;
  const dividerGap = hasBoth ? 10 : 0;
  const cardH = headerH + structSectionH + dividerGap + relSectionH + 12;

  const allValues = [...sRows, ...rRows].map(d => d.value);
  const maxVal = d3.max(allValues) || 1;

  const svgW = cardW + stubLen + 12;
  const svgH = cardH + 16;

  const svg = d3.select(container)
    .append('svg')
    .attr('width', svgW)
    .attr('height', svgH);

  const defs = svg.append('defs');

  // Drop shadow
  const shadow = defs.append('filter')
    .attr('id', 'card-shadow')
    .attr('x', '-6%').attr('y', '-3%')
    .attr('width', '112%').attr('height', '110%');
  shadow.append('feDropShadow')
    .attr('dx', 0).attr('dy', 3)
    .attr('stdDeviation', 6)
    .attr('flood-color', 'rgba(0,0,0,0.06)');

  // Arrow marker
  defs.append('marker')
    .attr('id', 'rel-arrow')
    .attr('viewBox', '0 0 8 6')
    .attr('refX', 8).attr('refY', 3)
    .attr('markerWidth', 6).attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M 0 0.5 L 7 3 L 0 5.5 z')
    .attr('fill', '#b8782d')
    .attr('opacity', 0.55);

  const g = svg.append('g').attr('transform', 'translate(0, 6)');

  // Card body
  g.append('rect')
    .attr('width', cardW).attr('height', cardH)
    .attr('rx', 12)
    .attr('fill', '#ffffff')
    .attr('stroke', '#e0dbd4')
    .attr('stroke-width', 1)
    .attr('filter', 'url(#card-shadow)');

  // Header tint (clipped to top rounded corners)
  const hClip = g.append('clipPath').attr('id', 'hdr-clip');
  hClip.append('rect').attr('width', cardW).attr('height', headerH).attr('rx', 12);
  g.append('rect')
    .attr('width', cardW).attr('height', headerH + 8)
    .attr('fill', color).attr('opacity', 0.09)
    .attr('clip-path', 'url(#hdr-clip)');

  // Accent bar
  g.append('rect')
    .attr('width', 4).attr('height', cardH)
    .attr('rx', 2).attr('fill', color);

  // Title
  g.append('text')
    .attr('x', 18).attr('y', headerH / 2 + 1)
    .attr('dominant-baseline', 'middle')
    .text(entry.label)
    .style('font-family', "'Instrument Serif', serif")
    .style('font-size', '17px')
    .style('fill', '#1a1a1a');

  // Total count
  g.append('text')
    .attr('x', cardW - 14).attr('y', headerH / 2 + 1)
    .attr('dominant-baseline', 'middle')
    .attr('text-anchor', 'end')
    .text(catData.count.toLocaleString() + ' entities')
    .style('font-family', "'JetBrains Mono', monospace")
    .style('font-size', '11px')
    .style('fill', '#8a8480');

  // Header border
  g.append('line')
    .attr('x1', 0).attr('y1', headerH)
    .attr('x2', cardW).attr('y2', headerH)
    .attr('stroke', '#e8e3dc');

  let y = headerH;

  // ── Helper: draw a module row with inline bar ──
  function drawRow(mod, yMid, barColor, textColor, showStub) {
    const pct = mod.value / catData.count;
    const bw = (mod.value / maxVal) * barMaxW;

    // Inline bar
    g.append('rect')
      .attr('x', barInset)
      .attr('y', yMid - rowH / 2 + 3)
      .attr('width', 0)
      .attr('height', rowH - 6)
      .attr('rx', 4)
      .attr('fill', barColor)
      .attr('opacity', 0.13)
      .transition().duration(500)
      .attr('width', bw);

    // Name
    g.append('text')
      .attr('x', barInset + 8).attr('y', yMid)
      .attr('dominant-baseline', 'middle')
      .text(mod.name)
      .style('font-family', "'DM Sans', sans-serif")
      .style('font-size', '12.5px')
      .style('fill', textColor);

    // Count + pct
    const countStr = formatCountShort(mod.value);
    const pctStr = (pct * 100).toFixed(1) + '%';
    g.append('text')
      .attr('x', cardW - 14).attr('y', yMid)
      .attr('dominant-baseline', 'middle')
      .attr('text-anchor', 'end')
      .style('font-family', "'JetBrains Mono', monospace")
      .style('font-size', '10.5px')
      .style('fill', '#a09a93')
      .text(`${countStr}  ${pctStr}`);

    // Edge stub for relational
    if (showStub) {
      g.append('line')
        .attr('x1', cardW + 2).attr('y1', yMid)
        .attr('x2', cardW + stubLen).attr('y2', yMid)
        .attr('stroke', '#b8782d')
        .attr('stroke-width', 1.5)
        .attr('opacity', 0.3)
        .attr('marker-end', 'url(#rel-arrow)');
    }
  }

  // ── Structural section ──
  if (sRows.length > 0) {
    y += 6;
    g.append('text')
      .attr('x', barInset + 2).attr('y', y + sectionLabelH / 2)
      .attr('dominant-baseline', 'middle')
      .text('PROPERTIES')
      .style('font-family', "'DM Sans', sans-serif")
      .style('font-size', '10px')
      .style('font-weight', '600')
      .style('letter-spacing', '1.2px')
      .style('fill', '#a09a93');
    y += sectionLabelH;

    sRows.forEach((mod, i) => {
      const yMid = y + i * rowH + rowH / 2;
      drawRow(mod, yMid, color, '#4a4540', false);
    });
    y += sBodyH;
  }

  // ── Divider ──
  if (hasBoth) {
    y += 5;
    g.append('line')
      .attr('x1', barInset).attr('y1', y)
      .attr('x2', cardW - barInset).attr('y2', y)
      .attr('stroke', '#e8e3dc');
    y += 5;
  }

  // ── Relational section ──
  if (rRows.length > 0) {
    g.append('text')
      .attr('x', barInset + 2).attr('y', y + sectionLabelH / 2)
      .attr('dominant-baseline', 'middle')
      .text('RELATIONS')
      .style('font-family', "'DM Sans', sans-serif")
      .style('font-size', '10px')
      .style('font-weight', '600')
      .style('letter-spacing', '1.2px')
      .style('fill', '#b8782d');
    y += sectionLabelH;

    rRows.forEach((mod, i) => {
      const yMid = y + i * rowH + rowH / 2;
      drawRow(mod, yMid, '#b8782d', '#9a6c2e', true);
    });
  }
}

// ============================================
// TREE VIEW
// ============================================
function renderTree(data) {
  treeRootEl.innerHTML = '';
  if (!data || typeof data !== 'object') {
    treeRootEl.innerHTML = '<div class="empty">No structured data found.</div>';
    return;
  }
  Object.entries(data).forEach(([key, value]) => {
    treeRootEl.appendChild(createNode(key, value, 0));
  });
}

function isQidOrPid(str) { return /^[QP]\d+$/.test(str); }

function createNode(key, value, depth) {
  if (isPlainObject(value)) {
    const details = document.createElement('details');
    details.className = 'node';
    details.open = depth < 1 || key === 'modules';
    const summary = document.createElement('summary');

    const label = document.createElement('span');
    label.textContent = isQidOrPid(key) ? idWithLabel(key) : key;

    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = `${Object.keys(value).length} keys`;

    summary.appendChild(label);
    summary.appendChild(meta);
    details.appendChild(summary);

    const children = document.createElement('div');
    children.className = 'children';
    Object.entries(value).forEach(([childKey, childValue]) => {
      children.appendChild(createNode(childKey, childValue, depth + 1));
    });
    details.appendChild(children);
    return details;
  }

  if (Array.isArray(value)) {
    const allPrimitive = value.every(item => !isPlainObject(item) && !Array.isArray(item));
    if (allPrimitive) return createArrayLeaf(key, value);

    const details = document.createElement('details');
    details.className = 'node';
    const summary = document.createElement('summary');
    const label = document.createElement('span');
    label.textContent = key;
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = `${value.length} items`;
    summary.appendChild(label);
    summary.appendChild(meta);
    details.appendChild(summary);

    const children = document.createElement('div');
    children.className = 'children';
    value.forEach((item, index) => {
      children.appendChild(createNode(`[${index}]`, item, depth + 1));
    });
    details.appendChild(children);
    return details;
  }

  return createValueLeaf(key, value);
}

function createArrayLeaf(key, values) {
  const wrapper = document.createElement('div');
  wrapper.className = 'leaf';

  const label = document.createElement('span');
  label.className = 'key';
  label.textContent = isQidOrPid(key) ? idWithLabel(key) : key;

  const chips = document.createElement('div');
  chips.className = 'chips inline';
  values.forEach(item => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    const str = String(item);
    chip.textContent = isQidOrPid(str) ? idWithLabel(str) : str;
    chips.appendChild(chip);
  });

  wrapper.appendChild(label);
  wrapper.appendChild(chips);
  return wrapper;
}

function createValueLeaf(key, value) {
  const wrapper = document.createElement('div');
  wrapper.className = 'leaf';

  const label = document.createElement('span');
  label.className = 'key';
  label.textContent = isQidOrPid(key) ? idWithLabel(key) : key;

  const valueEl = document.createElement('span');
  valueEl.className = 'value';
  valueEl.textContent = formatValue(value);

  wrapper.appendChild(label);
  wrapper.appendChild(valueEl);
  return wrapper;
}

function formatValue(value) {
  if (value === null || value === undefined) return 'present';
  if (typeof value === 'string') return value;
  return String(value);
}

function toggleAllNodes(container, open) {
  container.querySelectorAll('details').forEach(node => { node.open = open; });
}

function escapeHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function isPlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value);
}

init();
