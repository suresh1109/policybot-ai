/* PolicyBot Broker Portal JS v2 */

let _allLeads = [];
let _editLeadId = null;

/* ── Boot ── */
document.addEventListener('DOMContentLoaded', () => {
  loadAll();
  setInterval(loadAll, 60000);  // refresh every 60s
});

function showTab(name) {
  document.querySelectorAll('.broker-tab').forEach(t => t.classList.add('hidden'));
  document.querySelectorAll('.bn-link').forEach(l =>
    l.classList.toggle('active', l.dataset.tab === name));
  const el = document.getElementById(`tab-${name}`);
  if (el) el.classList.remove('hidden');
  if (name === 'leads')     renderLeadsTable(_allLeads);
  if (name === 'hot')       renderHotGrid();
  if (name === 'called')    renderCalledTable();
  if (name === 'converted') renderConvertedTable();
}

async function loadAll() {
  await Promise.all([loadStats(), loadLeads()]);
}

/* ══════════════════════════════════
   STATS
══════════════════════════════════ */
async function loadStats() {
  try {
    const r = await fetch('/api/broker/stats');
    const d = await r.json();
    if (d.status !== 'success') return;
    const s = d.stats;
    set('s-total',  s.total_leads);
    set('s-hot',    s.hot_leads);
    set('s-conv',   s.converted);
    set('s-rate',   s.conversion_rate + '%');
    set('s-recent', s.recent_7d);
    renderTypeBars(s.by_type || [], s.total_leads);
  } catch(e) { console.error('[stats]', e); }
}

function renderTypeBars(byType, total) {
  const el = document.getElementById('type-bars');
  if (!el || !byType.length) { if (el) el.innerHTML = '<div class="bk-loading">No data</div>'; return; }
  const max = Math.max(...byType.map(t => t.cnt), 1);
  el.innerHTML = byType.map(t => {
    const pct = Math.round(t.cnt / max * 100);
    return `<div class="type-bar-row">
      <div class="type-bar-label">${esc(t.insurance_type || 'Unknown')}</div>
      <div class="type-bar-track"><div class="type-bar-fill" style="width:${pct}%"></div></div>
      <div class="type-bar-cnt">${t.cnt}</div>
    </div>`;
  }).join('');
}

/* ══════════════════════════════════
   LEADS LOAD & RENDER
══════════════════════════════════ */
async function loadLeads() {
  try {
    const r = await fetch('/api/broker/leads');
    const d = await r.json();
    if (d.status !== 'success') return;
    _allLeads = d.leads || [];

    // Update badge
    const badge = document.getElementById('leads-count-badge');
    if (badge) badge.textContent = _allLeads.length;

    // Hot preview on dashboard
    renderHotPreview(_allLeads.filter(l => l.interest_level === 'high').slice(0, 5));
  } catch(e) { console.error('[leads]', e); }
}

function renderLeadsTable(leads) {
  const tbody = document.getElementById('leads-tbody');
  if (!tbody) return;
  if (!leads.length) {
    tbody.innerHTML = `<tr><td colspan="8">
      <div class="bk-empty"><i class="fas fa-users"></i><p>No leads yet</p></div>
    </td></tr>`;
    return;
  }
  tbody.innerHTML = leads.map(l => {
    const name = l.name || l.user_id?.slice(0,10) || '—';
    const risk = l.risk_category?.replace(' Risk','') || '—';
    return `<tr>
      <td>
        <div class="lt-name">${esc(name)}</div>
        <div class="lt-meta">${esc(l.city || '—')} · Age ${l.age || '—'}</div>
      </td>
      <td>${esc(l.insurance_type || '—')}</td>
      <td style="max-width:160px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(l.plan_name || '—')}</td>
      <td>${esc(l.premium_prediction || l.budget_range || '—')}</td>
      <td><span class="risk-chip ${risk}">${risk}</span></td>
      <td><span class="int-badge ${l.interest_level || 'low'}">${l.interest_level || 'low'}</span></td>
      <td><span class="status-badge ${l.lead_status || 'new'}">${l.lead_status || 'new'}</span></td>
      <td>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <button class="lt-action call" onclick="callLead(${l.id},'${esc(l.phone||'')}')">
            <i class="fas fa-phone"></i> Call
          </button>
          <button class="lt-action" onclick="openStatusModal(${l.id},'${esc(l.lead_status||'new')}','${esc(l.phone||'')}','${esc(l.best_call_time||'')}')">
            <i class="fas fa-pen"></i> Update
          </button>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function renderHotGrid() {
  const grid = document.getElementById('hot-grid');
  if (!grid) return;
  const hot = _allLeads.filter(l => l.interest_level === 'high');
  if (!hot.length) {
    grid.innerHTML = '<div class="bk-empty"><i class="fas fa-fire"></i><p>No hot leads yet — keep sharing the bot!</p></div>';
    return;
  }
  grid.innerHTML = hot.map(l => {
    const name = l.name || l.user_id?.slice(0,10) || 'Unknown';
    return `<div class="hot-card">
      <div class="hc-top">
        <div>
          <div class="hc-name">${esc(name)}</div>
          <div class="hc-city">${esc(l.city || '—')} · Age ${l.age || '—'}</div>
        </div>
        <div class="hc-fire">🔥</div>
      </div>
      <div class="hc-rows">
        <div class="hc-row"><span class="hc-row-lbl">Insurance</span><span class="hc-row-val">${esc(l.insurance_type||'—')}</span></div>
        <div class="hc-row"><span class="hc-row-lbl">Plan</span><span class="hc-row-val">${esc(l.plan_name||'—')}</span></div>
        <div class="hc-row"><span class="hc-row-lbl">Budget</span><span class="hc-row-val">${esc(l.budget_range||'—')}</span></div>
        <div class="hc-row"><span class="hc-row-lbl">Premium Est.</span><span class="hc-row-val">${esc(l.premium_prediction||'—')}</span></div>
        <div class="hc-row"><span class="hc-row-lbl">Status</span><span class="hc-row-val"><span class="status-badge ${l.lead_status||'new'}">${l.lead_status||'new'}</span></span></div>
        ${l.phone ? `<div class="hc-row"><span class="hc-row-lbl">Phone</span><span class="hc-row-val">${esc(l.phone)}</span></div>` : ''}
      </div>
      <div class="hc-actions">
        <button class="hc-btn call" onclick="callLead(${l.id},'${esc(l.phone||'')}')">
          <i class="fas fa-phone"></i> Call Now
        </button>
        <button class="hc-btn edit" onclick="openStatusModal(${l.id},'${esc(l.lead_status||'new')}','${esc(l.phone||'')}','${esc(l.best_call_time||'')}')">
          <i class="fas fa-pen"></i> Update
        </button>
      </div>
    </div>`;
  }).join('');
}

function renderHotPreview(leads) {
  const el = document.getElementById('hot-preview');
  if (!el) return;
  if (!leads.length) { el.innerHTML = '<div class="bk-loading" style="padding:16px 0">No hot leads yet</div>'; return; }
  el.innerHTML = leads.map(l => {
    const name = l.name || l.user_id?.slice(0,10) || 'Unknown';
    return `<div class="preview-row">
      <div>
        <div class="pr-name">${esc(name)}</div>
        <div class="pr-meta">${esc(l.city||'—')} · ${esc(l.insurance_type||'—')} · ${esc(l.plan_name||'—')}</div>
      </div>
      <div class="pr-right">
        <span class="status-badge ${l.lead_status||'new'}">${l.lead_status||'new'}</span>
        <button class="lt-action call" onclick="callLead(${l.id},'${esc(l.phone||'')}')">
          <i class="fas fa-phone"></i>
        </button>
      </div>
    </div>`;
  }).join('');
}

function renderCalledTable() {
  const tbody = document.getElementById('called-tbody');
  if (!tbody) return;
  const leads = _allLeads.filter(l => l.lead_status === 'called' || l.lead_status === 'follow_up');
  if (!leads.length) { tbody.innerHTML = '<tr><td colspan="6"><div class="bk-empty"><i class="fas fa-phone"></i><p>No called leads yet</p></div></td></tr>'; return; }
  tbody.innerHTML = leads.map(l => `<tr>
    <td><div class="lt-name">${esc(l.name||l.user_id?.slice(0,10)||'—')}</div><div class="lt-meta">${esc(l.city||'—')}</div></td>
    <td>${esc(l.plan_name||'—')}</td>
    <td>${esc(l.phone||'—')}</td>
    <td>${esc(l.best_call_time||'—')}</td>
    <td><span class="status-badge ${l.lead_status||'new'}">${l.lead_status||'new'}</span></td>
    <td><button class="lt-action" onclick="openStatusModal(${l.id},'${esc(l.lead_status||'')}','${esc(l.phone||'')}','${esc(l.best_call_time||'')}')"><i class="fas fa-pen"></i> Update</button></td>
  </tr>`).join('');
}

function renderConvertedTable() {
  const tbody = document.getElementById('converted-tbody');
  if (!tbody) return;
  const leads = _allLeads.filter(l => l.lead_status === 'converted');
  if (!leads.length) { tbody.innerHTML = '<tr><td colspan="5"><div class="bk-empty"><i class="fas fa-trophy"></i><p>No conversions yet — keep going! 💪</p></div></td></tr>'; return; }
  tbody.innerHTML = leads.map(l => `<tr>
    <td><div class="lt-name">${esc(l.name||l.user_id?.slice(0,10)||'—')}</div><div class="lt-meta">${esc(l.city||'—')}</div></td>
    <td>${esc(l.plan_name||'—')}</td>
    <td>${esc(l.insurance_type||'—')}</td>
    <td>${esc(l.budget_range||'—')}</td>
    <td style="color:#475569;font-size:12px">${(l.timestamp||'').slice(0,10)}</td>
  </tr>`).join('');
}

function filterLeads() {
  const q = (document.getElementById('leads-search')?.value||'').toLowerCase();
  const filtered = q ? _allLeads.filter(l =>
    (l.name||'').toLowerCase().includes(q) ||
    (l.city||'').toLowerCase().includes(q) ||
    (l.plan_name||'').toLowerCase().includes(q) ||
    (l.insurance_type||'').toLowerCase().includes(q)
  ) : _allLeads;
  renderLeadsTable(filtered);
}

/* ══════════════════════════════════
   ACTIONS
══════════════════════════════════ */
function callLead(id, phone) {
  if (phone) {
    window.open(`tel:${phone}`, '_self');
  }
  // Mark as called immediately
  updateLead(id, { lead_status: 'called' });
  // Update in-memory
  const lead = _allLeads.find(l => l.id === id);
  if (lead) lead.lead_status = 'called';
  brokerToast(`📞 Calling lead #${id}`, 'info');
}

function openStatusModal(id, status, phone, time) {
  _editLeadId = id;
  document.getElementById('modal-status').value = status || 'new';
  document.getElementById('modal-phone').value  = phone || '';
  document.getElementById('modal-time').value   = time  || '';
  document.getElementById('status-modal').classList.remove('hidden');
}

function closeStatusModal() {
  document.getElementById('status-modal').classList.add('hidden');
  _editLeadId = null;
}

async function saveLeadStatus() {
  if (!_editLeadId) return;
  const status = document.getElementById('modal-status').value;
  const phone  = document.getElementById('modal-phone').value.trim();
  const time   = document.getElementById('modal-time').value.trim();
  await updateLead(_editLeadId, { lead_status: status, phone, best_call_time: time });
  // Update in-memory
  const lead = _allLeads.find(l => l.id === _editLeadId);
  if (lead) { lead.lead_status = status; if (phone) lead.phone = phone; if (time) lead.best_call_time = time; }
  closeStatusModal();
  brokerToast('✅ Lead updated!', 'success');
  // Re-render current visible tab
  const activeTab = document.querySelector('.bn-link.active')?.dataset.tab;
  if (activeTab) showTab(activeTab);
}

async function updateLead(id, data) {
  try {
    await fetch(`/api/broker/lead/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    });
  } catch(e) { console.error('[updateLead]', e); }
}

/* ══════════════════════════════════
   UTILS
══════════════════════════════════ */
function set(id, val) { const el = document.getElementById(id); if (el) el.textContent = val ?? '—'; }

function esc(s) {
  return (s||'').toString()
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function brokerToast(msg, type='info') {
  let wrap = document.getElementById('broker-toast-wrap');
  if (!wrap) return;
  const t = document.createElement('div');
  const bg = type==='error'?'#ef4444':type==='success'?'#10b981':'#0ea5e9';
  t.style.cssText = `background:${bg};color:#fff;padding:10px 16px;border-radius:10px;
    font-size:13px;font-weight:600;box-shadow:0 6px 20px rgba(0,0,0,.4);
    animation:toastIn .3s ease;`;
  t.textContent = msg;
  wrap.appendChild(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(()=>t.remove(),300);},3500);
}
