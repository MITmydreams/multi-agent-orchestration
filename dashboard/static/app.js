/* ============================================================
 * TG Agent Command Center - Frontend
 * ============================================================ */

const API_BASE = 'http://localhost:8765';
const WS_URL   = 'ws://localhost:8765/ws/live';
const POLL_MS  = 5000;
const MAX_ACTIVITY = 50;

// ----- State ------------------------------------------------
const state = {
  overview: null,
  accounts: [],
  groups: [],
  activity: [],
  content: [],
  agents: [],
  filterRole: 'all',
  sortBy: 'activity',
  expanded: new Set(),
  ws: null,
  wsConnected: false,
  errorCount: 0,
};

// ----- 中文翻译映射 -----------------------------------------
const ROLE_CN = {
  infiltrator: '🕵️ 潜伏员',
  scout:       '🔍 侦察兵',
  content:     '📝 写手',
  backup:      '💤 替补',
};
const STATUS_CN = {
  active:      '在岗',
  nurturing:   '养号中',
  hibernating: '冬眠中',
  abandoned:   '已弃号',
};
const HEALTH_CN = {
  green:  '正常',
  yellow: '谨慎',
  red:    '暂停',
};
const CONTENT_TYPE_CN = {
  meme:          '段子',
  win_story:     '中奖故事',
  battle_report: '战报',
  review:        '评测',
};
const ACTIVITY_TYPE_CN = {
  message: '发言',
  task:    '任务',
  content: '产出',
  system:  '系统',
};
const ACTION_CN = {
  idle:       '待命中',
  lurking:    '潜水观察',
  scouting:   '侦察中',
  writing:    '撰写消息',
  posting:    '发布内容',
  sleeping:   '休眠中',
  evaluating: '评估群组',
};

function translateAction(action) {
  if (!action) return '待命中';
  const lower = String(action).toLowerCase();
  if (ACTION_CN[lower]) return ACTION_CN[lower];
  // 尝试翻译常见英文动作短语
  return action
    .replace(/sent message in/i, '发言于')
    .replace(/joined group/i, '加入群组')
    .replace(/scouted/i, '侦察了')
    .replace(/generated/i, '生成了')
    .replace(/reading/i, '阅读')
    .replace(/lurking in/i, '潜伏于')
    .replace(/idle/i, '待命中')
    .replace(/scouting/i, '侦察中');
}

// ----- Utilities --------------------------------------------
const $  = (id) => document.getElementById(id);
const escapeHtml = (s = '') => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function fmtTime(ts) {
  if (!ts) return '--:--:--';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '--:--:--';
  return d.toTimeString().slice(0, 8);
}
function fmtUptime(secs) {
  if (!secs && secs !== 0) return '--';
  secs = Math.floor(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h${String(m).padStart(2,'0')}m`;
  if (m > 0) return `${m}m${String(s).padStart(2,'0')}s`;
  return `${s}s`;
}
function initials(name = '?') {
  const parts = String(name).trim().split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}
function fmtNum(n) {
  if (n == null) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000)    return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

// ----- Fetch helpers ----------------------------------------
async function safeFetch(path) {
  try {
    const r = await fetch(API_BASE + path, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } catch (e) {
    state.errorCount++;
    return null;
  }
}

async function fetchOverview() {
  const data = await safeFetch('/api/overview');
  if (data) { state.overview = data; renderOverview(); }
}
async function fetchAccounts() {
  const data = await safeFetch('/api/accounts');
  if (data) { state.accounts = Array.isArray(data) ? data : (data.accounts || []); renderAgents(); }
}
async function fetchGroups() {
  const data = await safeFetch('/api/groups');
  if (data) { state.groups = Array.isArray(data) ? data : (data.groups || []); renderGroups(); }
}
async function fetchActivity() {
  const data = await safeFetch('/api/activity');
  if (data) {
    const arr = Array.isArray(data) ? data : (data.activity || data.items || []);
    state.activity = arr.slice(0, MAX_ACTIVITY);
    renderActivity();
  }
}
async function fetchContent() {
  const data = await safeFetch('/api/content');
  if (data) {
    const arr = Array.isArray(data) ? data : (data.content || data.items || []);
    state.content = arr.slice(0, 10);
    renderContent();
  }
}

// ----- Render: overview -------------------------------------
function renderOverview() {
  const o = state.overview;
  if (!o) return;
  const t = o.totals || {};
  const modeMap = { api: 'AI 模式', template: '模板模式' };
  $('aiMode').textContent  = modeMap[o.ai_mode] || (o.ai_mode || '--');
  $('cycleNum').textContent = o.current_cycle ?? 0;
  $('uptime').textContent   = fmtUptime(o.uptime_seconds);

  $('statAgents').textContent  = t.accounts ?? 0;
  $('statActive').textContent  = t.active   ?? 0;
  $('statGroups').textContent  = t.groups   ?? 0;
  $('statContent').textContent = fmtNum(t.content_pieces ?? 0);

  // Avg risk from accounts (fallback)
  let avgRisk = 0;
  if (state.accounts.length > 0) {
    avgRisk = state.accounts.reduce((s, a) => s + (a.risk_score || 0), 0) / state.accounts.length;
  }
  $('statRisk').textContent = avgRisk.toFixed(2);
  $('riskBarFill').style.width = `${Math.min(100, avgRisk * 100)}%`;

  // Health badge
  const health = (o.system_health || 'green').toLowerCase();
  const badge = $('healthBadge');
  badge.textContent = HEALTH_CN[health] || health;
  badge.className = 'health-badge health-' + health;

  $('lastUpdate').textContent = new Date().toTimeString().slice(0, 8);
}

// ----- Render: agent fleet ----------------------------------
function sortAccounts(list) {
  const arr = [...list];
  switch (state.sortBy) {
    case 'risk':     arr.sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0)); break;
    case 'role':     arr.sort((a, b) => (a.role || '').localeCompare(b.role || '')); break;
    case 'activity':
    default:         arr.sort((a, b) => (b.messages_today || 0) - (a.messages_today || 0));
  }
  return arr;
}
function renderAgents() {
  const grid = $('agentGrid');
  let list = state.accounts;
  if (state.filterRole !== 'all') list = list.filter(a => (a.role || '') === state.filterRole);
  list = sortAccounts(list);

  if (list.length === 0) {
    grid.innerHTML = '<div class="empty-state">没有匹配的队员</div>';
    return;
  }
  grid.innerHTML = list.map(renderAgentCard).join('');

  // Wire up clicks
  grid.querySelectorAll('.agent-card').forEach(el => {
    el.addEventListener('click', () => {
      const id = el.dataset.id;
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      el.classList.toggle('expanded');
    });
  });

  // Update overview risk avg if rendered after accounts
  if (state.overview) renderOverview();
}
function renderAgentCard(a) {
  const role = a.role || 'backup';
  const status = a.status || 'active';
  const expanded = state.expanded.has(String(a.id)) ? 'expanded' : '';
  return `
    <div class="agent-card role-${role} ${expanded}" data-id="${a.id}">
      <div class="agent-head">
        <div class="avatar role-${role}">
          ${initials(a.display_name)}
          <span class="status-dot s-${status}" title="${STATUS_CN[status] || status}"></span>
        </div>
        <div class="agent-info">
          <div class="agent-name">${escapeHtml(a.display_name || '未命名')}</div>
          <div class="agent-handle">@${escapeHtml(a.username || '---')}</div>
        </div>
      </div>
      <span class="role-badge role-${role}">${ROLE_CN[role] || role}</span>
      <div class="agent-stats">
        <div class="agent-stat">
          <div class="agent-stat-val">${a.messages_today ?? 0}</div>
          <div class="agent-stat-label">今日发言</div>
        </div>
        <div class="agent-stat">
          <div class="agent-stat-val">${(a.risk_score ?? 0).toFixed(2)}</div>
          <div class="agent-stat-label">风险度</div>
        </div>
        <div class="agent-stat">
          <div class="agent-stat-val">${(a.trust_score ?? 0).toFixed(2)}</div>
          <div class="agent-stat-label">信任度</div>
        </div>
      </div>
      <div class="agent-action">📍 ${escapeHtml(translateAction(a.current_action))}</div>
      <div class="agent-expand">
        <div><span>状态</span><strong>${STATUS_CN[status] || status}</strong></div>
        <div><span>累计发言</span><strong>${a.total_messages ?? 0}</strong></div>
        <div><span>今日推广</span><strong>${a.promo_today ?? 0}</strong></div>
        <div><span>活跃群数</span><strong>${a.groups_today ?? 0}</strong></div>
        <div><span>被踢次数</span><strong>${a.kicked_count ?? 0}</strong></div>
        <div><span>号龄</span><strong>${a.age_days ?? 0}天</strong></div>
        <div><span>代理</span><strong>#${a.proxy_id ?? '-'}</strong></div>
        <div><span>最后活跃</span><strong>${fmtTime(a.last_active)}</strong></div>
      </div>
    </div>
  `;
}

// ----- Render: activity feed --------------------------------
function renderActivity() {
  const feed = $('activityFeed');
  $('activityCount').textContent = `${state.activity.length} 条事件`;
  if (state.activity.length === 0) {
    feed.innerHTML = '<div class="empty-state">等待事件中...</div>';
    return;
  }
  feed.innerHTML = state.activity.map(renderActivityItem).join('');
}
function renderActivityItem(it) {
  const type = (it.type || 'message').toLowerCase();
  const t = fmtTime(it.timestamp).slice(0, 8);
  const typeLabel = ACTIVITY_TYPE_CN[type] || type;
  return `
    <div class="activity-item t-${type}">
      <div class="activity-time">${t}</div>
      <div class="activity-body">
        <div class="activity-line"><span class="agent">${escapeHtml(it.agent_name || '系统')}</span> ${escapeHtml(translateAction(it.action) || typeLabel)}</div>
        ${it.details ? `<div class="activity-details">${escapeHtml(it.details)}</div>` : ''}
      </div>
    </div>
  `;
}

// ----- Render: content stream -------------------------------
function renderContent() {
  const el = $('contentStream');
  $('contentCount').textContent = `${state.content.length} 条`;
  if (state.content.length === 0) {
    el.innerHTML = '<div class="empty-state">暂无内容...</div>';
    return;
  }
  const LANG_CN = { en: '英文', zh: '中文', ja: '日文', ko: '韩文' };
  el.innerHTML = state.content.map(c => {
    const type = (c.type || c.content_type || 'meme').toLowerCase();
    const lang = c.language || c.lang || 'en';
    const text = c.content || c.text || c.snippet || '';
    const spam = (c.spam_score ?? c.spamScore ?? 0).toFixed ? (c.spam_score ?? 0).toFixed(2) : c.spam_score || '0.00';
    return `
      <div class="content-item c-${type}">
        <div class="content-head">
          <span class="content-type c-${type}">${CONTENT_TYPE_CN[type] || type}</span>
          <span class="content-lang">${LANG_CN[lang] || lang}</span>
          <span class="content-spam">垃圾度: ${spam}</span>
        </div>
        <div class="content-snippet">${escapeHtml(text)}</div>
      </div>
    `;
  }).join('');
}

// ----- Render: groups table ---------------------------------
function renderGroups() {
  const tb = $('groupsTbody');
  $('groupCount').textContent = `${state.groups.length} 个群`;
  if (state.groups.length === 0) {
    tb.innerHTML = '<tr><td colspan="7" class="empty-state">暂无群组，等待侦察兵发现目标...</td></tr>';
    return;
  }
  const GROUP_STATUS_CN = {
    active:       '运营中',
    infiltrating: '渗透中',
    evaluated:    '已评估',
    cooldown:     '冷却中',
    banned:       '已被封',
  };
  const LANG_CN = { en: '英文', zh: '中文', ja: '日文', ko: '韩文' };
  tb.innerHTML = state.groups.map(g => {
    const grade = (g.grade || 'C').toUpperCase();
    const status = g.status || 'unknown';
    const statusClass = status === 'active' || status === 'ok' ? 'status-ok'
                      : status === 'warning' || status === 'cooldown' ? 'status-warn' : 'status-bad';
    const statusIcon = status === 'active' || status === 'ok' ? '✅'
                      : status === 'warning' || status === 'cooldown' ? '⚠️' : '⛔';
    const agentCount = g.agent_count ?? (g.agents ? g.agents.length : 0);
    return `
      <tr>
        <td><strong>${escapeHtml(g.title || '未命名')}</strong></td>
        <td class="mono">@${escapeHtml(g.username || '---')}</td>
        <td><span class="grade grade-${grade}">${grade}</span></td>
        <td class="mono">${fmtNum(g.member_count ?? 0)}</td>
        <td class="mono">${LANG_CN[g.language] || g.language || '英文'}</td>
        <td class="mono">${agentCount} 人</td>
        <td class="${statusClass}">${statusIcon} ${GROUP_STATUS_CN[status] || status}</td>
      </tr>
    `;
  }).join('');
}

// ----- Error banner -----------------------------------------
function checkErrors() {
  const banner = $('errorBanner');
  if (state.errorCount > 0 && !state.overview && !state.accounts.length) {
    banner.classList.remove('hidden');
  } else if (state.overview || state.accounts.length) {
    banner.classList.add('hidden');
  }
}
$('errorDismiss').addEventListener('click', () => $('errorBanner').classList.add('hidden'));

// ----- WebSocket --------------------------------------------
function connectWS() {
  try {
    const ws = new WebSocket(WS_URL);
    state.ws = ws;
    ws.onopen = () => {
      state.wsConnected = true;
      $('liveStatus').textContent = '实时连接';
      $('livePill').classList.remove('disconnected');
    };
    ws.onclose = () => {
      state.wsConnected = false;
      $('liveStatus').textContent = '已断开';
      $('livePill').classList.add('disconnected');
      setTimeout(connectWS, 5000);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        handleWSMessage(msg);
      } catch (e) {}
    };
  } catch (e) {
    setTimeout(connectWS, 5000);
  }
}
function handleWSMessage(msg) {
  if (!msg || !msg.type) return;
  switch (msg.type) {
    case 'activity':
    case 'message':
    case 'task':
    case 'content_event':
      if (msg.data) {
        state.activity.unshift(msg.data);
        if (state.activity.length > MAX_ACTIVITY) state.activity.pop();
        renderActivity();
      }
      break;
    case 'overview':
      state.overview = msg.data; renderOverview(); break;
    case 'account_update':
      if (msg.data) {
        const i = state.accounts.findIndex(a => a.id === msg.data.id);
        if (i >= 0) state.accounts[i] = { ...state.accounts[i], ...msg.data };
        renderAgents();
      }
      break;
    case 'content':
      if (msg.data) {
        state.content.unshift(msg.data);
        if (state.content.length > 10) state.content.pop();
        renderContent();
      }
      break;
  }
}

// ----- Filter / sort wiring ---------------------------------
document.querySelectorAll('#roleFilter .filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#roleFilter .filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.filterRole = btn.dataset.role;
    renderAgents();
  });
});
$('sortSelect').addEventListener('change', (e) => {
  state.sortBy = e.target.value;
  renderAgents();
});

// ----- Refresh loop -----------------------------------------
async function refreshAll() {
  await Promise.all([
    fetchOverview(),
    fetchAccounts(),
    fetchActivity(),
    fetchContent(),
    fetchGroups(),
  ]);
  checkErrors();
}

// ----- Boot --------------------------------------------------
(async function boot() {
  $('liveStatus').textContent = '连接中';
  await refreshAll();
  setInterval(refreshAll, POLL_MS);
  connectWS();
})();
