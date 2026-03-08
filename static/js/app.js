/* PolicyBot v6 — Full debug pass
   FIXED:
   1. autoSelectDocType was missing its function declaration (loose code crashed JS on load)
   2. startBot now reliably shows welcome message + insurance type buttons
   3. After Gov ID verified: confirms name, age, gender match against ID before continuing
   4. verif-row badge shown/hidden correctly
   5. setButtonsEnabled always re-enables on error/finally
*/

// ─── State ───────────────────────────────────────────────────────────────────
// NEW: userId is regenerated EVERY page load — ensures zero DB collision between sessions
let userId      = genId();
let sessionId   = genId();
let currentTheme = localStorage.getItem('pb_theme') || 'neon';
let currentLang  = localStorage.getItem('pb_lang')  || 'English';
let currentStage = 'insurance_type';
let isFirstMessage = true;    // ← flag: first real message triggers session reset on backend
let selectedStar = 0;
let multiSelections = {};
let isListening  = false;
let recognition  = null;
let largeFontOn  = false;
let planNodes3D = [];  // stores current plan options for 3D highlighting
let buttonsDisabled = false;  // MUST be declared — prevents ReferenceError
let ttsEnabled   = false;   // TTS toggle state
let ttsUtterance = null;    // current TTS utterance

const DOT_STAGES = [
  'insurance_type','collect_name','collect_age','doc_upload',
  'collect_city',
  'collect_coverage','collect_family_count','collect_family_medical',
  'collect_medical_status','collect_medical',
  'collect_budget','review_details',
  'recommendation','ask_rating'
];

const UPLOAD_STAGES = [
  'doc_upload','verify_wait',
  'optional_medical_report',
  'condition_report_upload','condition_report_wait',
  'optional_health_check','vehicle_history','vehicle_doc_upload',
  'life_docs','travel_declare','property_history'
];

const SKIPPABLE_STAGES = [
  'optional_medical_report',
  'optional_health_check','vehicle_doc_upload','life_docs',
  'travel_declare','property_history','condition_report_upload','vehicle_history'
];

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  applyTheme(currentTheme);
  document.querySelectorAll('.th-btn').forEach(b =>
    b.addEventListener('click', () => applyTheme(b.dataset.theme))
  );
  setupDragDrop();
  initVoice();
  initTTS();
  initParticles();

  // ── Check if landing screen exists — if so, show it first ────────────
  const landing = document.getElementById('landing-screen');
  const shell   = document.getElementById('floating-shell');
  if (landing && shell) {
    initLanding3D();
    // Chat NOT started yet — landing is shown, shell is hidden
    // startBot() will be called by launchChat()
  } else {
    startBot();
  }
});

// ── Landing 3D WebGL scene ────────────────────────────────────────────────────
function initLanding3D() {
  const canvas = document.getElementById('landing-3d');
  if (!canvas) return;
  const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
  if (!gl) return;

  function resize() {
    canvas.width  = window.innerWidth;
    canvas.height = window.innerHeight;
    gl.viewport(0, 0, canvas.width, canvas.height);
  }
  resize();
  window.addEventListener('resize', resize);

  // Vertex shader — full-screen quad
  const VS = `
    attribute vec2 a_pos;
    void main(){ gl_Position = vec4(a_pos,0,1); }
  `;
  // Fragment shader — animated plasma / galaxy field
  const FS = `
    precision highp float;
    uniform vec2  u_res;
    uniform float u_time;

    float hash(vec2 p){ return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453); }
    float noise(vec2 p){
      vec2 i=floor(p), f=fract(p), u=f*f*(3.-2.*f);
      return mix(mix(hash(i),hash(i+vec2(1,0)),u.x),
                 mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),u.x),u.y);
    }
    float fbm(vec2 p){
      float v=0.;float a=.5;
      for(int i=0;i<5;i++){v+=a*noise(p);p*=2.;a*=.5;}
      return v;
    }

    void main(){
      vec2 uv = (gl_FragCoord.xy - u_res*.5) / min(u_res.x,u_res.y);
      float t = u_time * 0.3;

      // Swirling plasma layers
      float f1 = fbm(uv*2.5 + vec2(t*.4, t*.3));
      float f2 = fbm(uv*2.0 - vec2(t*.2, t*.5) + f1);
      float f3 = fbm(uv*1.5 + vec2(f2, f1) + t*.1);

      // Color palettes: deep space purple-blue-cyan
      vec3 c1 = vec3(0.08,0.04,0.22);  // deep indigo
      vec3 c2 = vec3(0.22,0.08,0.45);  // purple
      vec3 c3 = vec3(0.05,0.18,0.40);  // deep blue
      vec3 c4 = vec3(0.0 ,0.55,0.65);  // cyan glow

      vec3 col = mix(c1, c2, f1);
      col = mix(col, c3, f2*.6);
      col = mix(col, c4, f3*f3*.4);

      // Stars
      vec2 sv = uv * 180.;
      float star = pow(max(0., 1.-length(fract(sv)-.5)*6.), 5.);
      star *= hash(floor(sv)) > 0.93 ? 1. : 0.;
      col += star * .7 * vec3(.8,.9,1.);

      // Vignette
      float vig = 1. - dot(uv*1.2, uv*1.2);
      col *= max(0., vig);

      gl_FragColor = vec4(col, 1.0);
    }
  `;

  function compile(src, type) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    return s;
  }
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(VS, gl.VERTEX_SHADER));
  gl.attachShader(prog, compile(FS, gl.FRAGMENT_SHADER));
  gl.linkProgram(prog);
  gl.useProgram(prog);

  // Full-screen quad
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW);
  const aPOS = gl.getAttribLocation(prog,'a_pos');
  gl.enableVertexAttribArray(aPOS);
  gl.vertexAttribPointer(aPOS, 2, gl.FLOAT, false, 0, 0);

  const uRES  = gl.getUniformLocation(prog,'u_res');
  const uTIME = gl.getUniformLocation(prog,'u_time');

  let start = Date.now();
  let animId;
  function frame() {
    // Stop rendering if landing is gone
    if (!document.getElementById('landing-screen') ||
        document.getElementById('landing-screen').style.display === 'none') {
      cancelAnimationFrame(animId); return;
    }
    gl.uniform2f(uRES, canvas.width, canvas.height);
    gl.uniform1f(uTIME, (Date.now()-start)/1000);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    animId = requestAnimationFrame(frame);
  }
  frame();
}

// ── Launch chat from landing screen ──────────────────────────────────────────
function _doLaunchTransition(preselect, startWithVoice) {
  const landing = document.getElementById('landing-screen');
  const shell   = document.getElementById('floating-shell');
  if (!landing || !shell) return;
  landing.classList.add('hiding');
  setTimeout(() => {
    landing.style.display = 'none';
    shell.classList.remove('hidden');
    shell.classList.add('chat-launching');
    startBot();
    if (preselect) {
      setTimeout(() => doSend(preselect, preselect), 800);
    } else if (startWithVoice) {
      // Launch voice immediately after bot greeting appears
      setTimeout(() => {
        const shell2 = document.getElementById('floating-shell');
        if (shell2) startVoice();
      }, 900);
    }
  }, 400);
}

function launchChat(preselect) {
  _doLaunchTransition(preselect, false);
}

function launchChatVoice() {
  // Pulse the orb, then transition to chat with voice on
  const orb = document.getElementById('lp-orb');
  if (orb) orb.classList.add('listening');
  setTimeout(() => _doLaunchTransition(null, true), 600);
}

function launchChatWith(insuranceType) {
  launchChat(insuranceType);
}

function genId() {
  return 'pb_' + Math.random().toString(36).slice(2,10) + Date.now().toString(36);
}

// ─── Theme ────────────────────────────────────────────────────────────────────
function applyTheme(t) {
  currentTheme = t;
  const root = document.getElementById('body-root') || document.documentElement;
  // Remove all theme classes first
  root.className = root.className.replace(/\btheme-\S+/g, '').trim();
  root.classList.add(`theme-${t}`);
  localStorage.setItem('pb_theme', t);
  document.querySelectorAll('.th-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.theme === t)
  );
}
function toggleFontSize() {
  largeFontOn = !largeFontOn;
  document.body.classList.toggle('large-font', largeFontOn);
}
function setLang(lang) {
  currentLang = lang;
  localStorage.setItem('pb_lang', lang);
  if (recognition) recognition.lang = getLangCode(lang);
  // Sync both lang dropdowns
  const selA = document.getElementById('lang-sel');
  const selB = document.getElementById('topbar-lang');
  if (selA) selA.value = lang;
  if (selB) selB.value = lang;
  showToast(`Language: ${lang}`, 'info');
}

// ─── Start bot ────────────────────────────────────────────────────────────────
function startBot() {
  // Reset flags for fresh session
  isFirstMessage = true;
  currentStage   = 'insurance_type';

  // Reset all UI to clean state
  const pName = document.getElementById('p-name');
  if (pName) pName.textContent = 'Your Profile';

  ['pf-ins','pf-age','pf-city','pf-budget','pf-medical'].forEach(id => {
    document.getElementById(id)?.classList.add('hidden');
  });

  document.getElementById('id-verified-dot')?.classList.add('hidden');
  document.getElementById('verif-row')?.classList.add('hidden');
  document.getElementById('upload-card')?.classList.add('hidden');
  document.getElementById('upload-section-wrap')?.classList.add('hidden');
  document.getElementById('confidence-card')?.classList.add('hidden');
  document.getElementById('chat-messages').innerHTML = '';
  document.getElementById('options-zone').innerHTML  = '';

  const badge = document.getElementById('vb-govid');
  if (badge) { badge.className = 'verif-badge pending'; badge.textContent = 'Pending'; }

  // Load XP so bar shows immediately (not waiting for first stage event)
  fetch('/api/xp/status', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({user_id: userId})
  }).then(r => r.json()).then(xd => {
    if (xd.status === 'success') updateXPBar(xd.xp_total, xd.level_name, xd.level_icon, xd.next_thresh);
  }).catch(() => {});

  // Show welcome + insurance type buttons immediately — NO backend call here
  // Backend reset happens on the FIRST real message (is_new_chat:true)
  addBotBubble(
    "Hi 😊 I'm PolicyBot, your AI insurance advisor!\nWhat type of insurance are you looking for today?",
    ["Health Insurance","Term / Life Insurance","Vehicle Insurance",
     "Travel Insurance","Property Insurance","Accident Insurance"],
    "radio"
  );
  updateProgress(7, 'Insurance Type', 'insurance_type');
  setButtonsEnabled(true);
}

// ─── Send message ─────────────────────────────────────────────────────────────
async function sendMsg() {
  const inp  = document.getElementById('chat-input');
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  autoH(inp);
  await doSend(text, null);
}

async function doSend(text, selectedOption) {
  if (buttonsDisabled) return;
  // Guard: health_report_confirm is a pure-frontend pseudo-stage
  // Typing here must do nothing — user must use the two buttons
  if (currentStage === 'health_report_confirm') { setButtonsEnabled(true); return; }
  setButtonsEnabled(false);
  clearOptions();
  document.getElementById('suggestion-chips')?.remove();
  addUserBubble(selectedOption || text);
  showTyping(true);

  // is_new_chat is true ONLY on the very first message of a session
  // This triggers backend reset (profile + history wipe) atomically with the real message
  const newChat = isFirstMessage;
  if (isFirstMessage) isFirstMessage = false;

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        message:         text,
        selected_option: selectedOption,
        user_id:         userId,
        session_id:      sessionId,
        language:        currentLang,
        is_new_chat:     newChat       // ← true only on first message
      })
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    showTyping(false);

    if (d.status === 'success') {
      const _prevStage = currentStage;
      currentStage = d.stage;
      // Only dispatch stage event when stage actually changes — prevents duplicate XP calls
      if (d.stage && d.stage !== _prevStage) {
        document.dispatchEvent(new CustomEvent('policybot:stage', {detail:{stage:d.stage, userId}}));
      }
      addBotBubble(d.reply, d.options || [], d.option_type || 'none');
      renderSuggestions(d.stage);
      updateProgress(d.progress, d.stage_label, d.stage);
      setTimeout(fetchMemory, 300);
      refreshProfile();

      // ── review_details: highlight the review card ───────────────────────
      if (d.stage === 'review_details') {
        // Briefly flash the profile card to draw attention
        const profileCard = document.querySelector('.profile-card');
        if (profileCard) {
          profileCard.classList.add('pulse-highlight');
          setTimeout(() => profileCard.classList.remove('pulse-highlight'), 2000);
        }
      }

      // ── recommendation: show comparison table + action buttons ────────
      if (d.stage === 'recommendation' && d.module === 'recommendation') {
        showToast('🔍 Profile analyzed — showing best plans for you!', 'success');
        // Load hospital network for health insurance
        fetch(`/api/profile?user_id=${encodeURIComponent(userId)}`)
          .then(r=>r.json()).then(p=>{
            const prof = p.profile || {};
            if (prof.city && prof.insurance_type) {
              setTimeout(() => loadHospitalNetwork(prof.city, prof.insurance_type), 1200);
            }
          }).catch(()=>{});
        // Inject comparison table (only once per session)
        if (!document.getElementById('plan-compare-table') && d.plans_table && d.plans_table.length) {
          setTimeout(() => injectPlanCompareTable(d.plans_table), 700);
        }
        // Inject action buttons (Download + WhatsApp)
        if (!document.getElementById('inline-report-btn')) {
          setTimeout(() => injectReportButtons(), 1400);
        }
      }
      // Upload card visibility
      const uploadCard = document.getElementById('upload-card');
      const uploadWrap = document.getElementById('upload-section-wrap');
      if (d.show_upload || UPLOAD_STAGES.includes(d.stage)) {
        uploadWrap?.classList.remove('hidden');
        uploadCard?.classList.remove('hidden');
        autoSelectDocType(d.stage);
        updateSkipButton(d.stage);
        document.getElementById('upload-drop-zone')?.classList.add('attention');
        setTimeout(() => uploadWrap?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 150);
        if (d.stage === 'doc_upload' || d.stage === 'verify_wait') {
          document.getElementById('verif-row')?.classList.remove('hidden');
        }
      } else {
        uploadCard?.classList.add('hidden');
        uploadWrap?.classList.add('hidden');
        document.getElementById('upload-drop-zone')?.classList.remove('attention');
      }

      if (d.confidence) showConfidence(d.confidence);

      // 3D visualizer — show at recommendation stage
      if (d.stage === 'recommendation') {
        show3DVisualizer(d.options || []);
        planNodes3D = d.options || [];
        showSessionQuickBar();
      }
      // Hide 3D when past recommendation
      if (['explain_plan','ask_escalation','ask_rating','farewell'].includes(d.stage)) {
        hide3DVisualizer();
        hideSessionQuickBar();
      }

      if (d.show_escalation) {
        document.getElementById('esc-panel')?.classList.remove('hidden');
      }

      // Rating stage: clear option buttons FIRST, then show rating panel
      if (d.stage === 'ask_rating' || d.show_rating) {
        clearOptions(false);              // wipe plan radio buttons
        setTimeout(() => showRatingPanel(), 180);  // then show star widget
      }

      if (d.show_farewell || d.stage === 'farewell' || d.lock_chat) {
        if (d.show_farewell || d.stage === 'farewell') {
          setTimeout(() => showFarewell(d.reply), 800);
          triggerCleanup();
        }
        lockChatInput();
      }

      // Farewell: clear everything including rating panel
      if (d.stage === 'farewell') {
        setTimeout(() => clearOptions(true), 100);
      }
    } else {
      showTyping(false);
      addBotBubble("Sorry, I had a hiccup. Please try again! 🙏");
    }
  } catch(e) {
    showTyping(false);
    addBotBubble("Connection issue — please check your internet and try again.");
    console.error('[doSend error]', e);
  } finally {
    setButtonsEnabled(true);
  }
}

// ─── Enable / Disable buttons ─────────────────────────────────────────────────
function setButtonsEnabled(enabled) {
  buttonsDisabled = !enabled;
  document.querySelectorAll('.opt-btn, .multi-confirm-btn').forEach(btn => {
    btn.disabled = !enabled;
    btn.classList.toggle('disabled', !enabled);
  });
  const inp = document.getElementById('chat-input');
  if (inp) inp.disabled = !enabled;
  const sendBtn = document.querySelector('.send-btn');
  if (sendBtn) sendBtn.disabled = !enabled;
}

// ─── Permanently lock chat after farewell ─────────────────────────────────────
function lockChatInput() {
  buttonsDisabled = true;
  clearOptions();
  // Disable text input
  const inp = document.getElementById('chat-input');
  if (inp) {
    inp.disabled = true;
    inp.placeholder = '🎉 Conversation complete — start a new chat!';
    inp.style.opacity = '0.5';
    inp.style.cursor  = 'not-allowed';
  }
  // Disable send button
  const sendBtn = document.querySelector('.send-btn');
  if (sendBtn) { sendBtn.disabled = true; sendBtn.style.opacity = '0.4'; }
  // Disable mic button
  const micBtn = document.querySelector('.mic-btn');
  if (micBtn) { micBtn.disabled = true; micBtn.style.opacity = '0.4'; }
  // Hide upload card
  document.getElementById('upload-card')?.classList.add('hidden');
  document.getElementById('upload-section-wrap')?.classList.add('hidden');
  // Clear options zone completely
  const oz = document.getElementById('options-zone');
  if (oz) oz.innerHTML = '';
  // Show a "new chat" prompt in the options zone
  if (oz) {
    oz.innerHTML = '<button class="opt-btn new-chat-btn" onclick="startNewChat()" style="background:linear-gradient(135deg,var(--a1),var(--a2));color:#fff;border:none;padding:10px 28px;border-radius:30px;font-weight:600;font-size:13px;cursor:pointer;margin-top:8px;box-shadow:0 4px 14px rgba(90,114,255,.4)">🔄 Start New Chat</button>';
  }
}

// ─── Auto-select doc type based on stage ─────────────────────────────────────
function autoSelectDocType(stage) {
  const sel = document.getElementById('up-doc-type');
  if (!sel) return;
  const map = {
    'doc_upload':              'aadhaar',
    'verify_wait':             'aadhaar',
    'optional_medical_report': 'medical_report',
    'condition_report_upload': 'health_report',
    'condition_report_wait':   'health_report',
    'optional_health_check':   'health_report',
    'vehicle_history':         'vehicle_insurance',
    'vehicle_doc_upload':      'vehicle_insurance',
    'life_docs':               'life_doc',
    'travel_declare':          'travel_doc',
    'property_history':        'property_doc',
  };
  const val = map[stage];
  if (val) sel.value = val;
}

// ─── Skip button visibility ───────────────────────────────────────────────────
function updateSkipButton(stage) {
  const skipBtn = document.getElementById('skip-upload-btn');
  if (!skipBtn) return;
  if (SKIPPABLE_STAGES.includes(stage)) {
    skipBtn.classList.remove('hidden');
    skipBtn.disabled = false;
  } else {
    skipBtn.classList.add('hidden');
  }
}

// ─── Skip condition upload ────────────────────────────────────────────────────
async function skipConditionUpload() {
  const skipBtn = document.getElementById('skip-upload-btn');
  if (skipBtn) skipBtn.disabled = true;
  await doSend('skip', 'Skip — Continue Without');
}

// ─── Drag & Drop setup ────────────────────────────────────────────────────────
function setupDragDrop() {
  const zone = document.getElementById('upload-drop-zone');
  if (!zone) return;
  zone.addEventListener('dragover', e => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file) processUpload(file);
  });
}

function handleUpload(input) {
  const file = input.files[0];
  if (file) processUpload(file);
  input.value = '';
}

// ─── Document Upload Pipeline ─────────────────────────────────────────────────
async function processUpload(file) {
  const sel = document.getElementById('up-doc-type');
  const docType = sel ? sel.value : 'aadhaar';
  const ext = file.name.split('.').pop().toLowerCase();

  const ALLOWED = ['jpg','jpeg','png','webp','gif','bmp','tiff','tif','pdf','docx','doc','txt'];
  if (!ALLOWED.includes(ext)) {
    showToast('Unsupported file type. Use JPG, PNG, WEBP, PDF, DOCX, or TXT.', 'error');
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    showToast('File too large. Maximum 15MB allowed.', 'error');
    return;
  }
  if (file.size < 500) {
    showToast('File appears empty. Please try again.', 'error');
    return;
  }

  setButtonsEnabled(false);
  clearOptions();
  showVerifOverlay(docType);
  // Remove attention pulse — user has started uploading
  document.getElementById('upload-drop-zone')?.classList.remove('attention');

  const form = new FormData();
  form.append('file',       file);
  form.append('doc_type',   docType);
  form.append('user_id',    userId);
  form.append('session_id', sessionId);

  try {
    setVerifStep(1); await sleep(350);
    setVerifStep(2);

    const res = await fetch('/api/upload', { method: 'POST', body: form });

    await sleep(450);
    setVerifStep(3);
    await sleep(600);
    setVerifStep(4);

    const d = await res.json();
    d.doc_type = docType;

    await sleep(400);
    setVerifStep(5);
    await sleep(450);

    hideVerifOverlay();
    handleVerifResult(d);

  } catch(e) {
    hideVerifOverlay();
    showToast('Upload failed — please check your connection and try again.', 'error');
    console.error('[processUpload error]', e);
    setButtonsEnabled(true);
  }
}

// ─── Handle verification result ───────────────────────────────────────────────
function handleVerifResult(d) {
  clearOptions();

  // Show status box in sidebar
  document.getElementById('verif-status-box')?.classList.remove('hidden');
  const vsresult = document.getElementById('vsb-result');
  if (vsresult) vsresult.classList.remove('hidden');

  // Engine badge
  const ocrBadge = document.getElementById('ocr-badge');
  if (ocrBadge) ocrBadge.textContent = d.engine === 'offline_ocr' ? '🔍 OCR' : '🤖 AI';

  const isConditionDoc = [
    'medical_report','health_report','condition_report','vehicle_insurance',
    'rc_book','life_doc','travel_doc','property_doc'
  ].includes(d.doc_type);

  if (d.verified && isConditionDoc) {
    // Store for PDF download
    window._lastReportData = d;

    const ic    = d.identity_check || {};
    const risk  = d.risk_level || 'LOW';
    const conds = d.conditions_found || [];
    const riskColor = { LOW:'#16a34a', MEDIUM:'#d97706', HIGH:'#dc2626' };
    const riskEmoji = { LOW:'🟢', MEDIUM:'🟡', HIGH:'🔴' };

    function matchRow(label, val) {
      const v = (val||'UNKNOWN').toUpperCase();
      const icon  = v==='YES' ? '✅' : v==='NO' ? '⚠️' : '❓';
      const color = v==='YES' ? '#16a34a' : v==='NO' ? '#dc2626' : '#d97706';
      return `<tr>
        <td class="hrc-lbl">${label}</td>
        <td class="hrc-val" style="color:${color};font-weight:600">${icon}&nbsp;${v}</td>
      </tr>`;
    }

    const cardHtml = `
<div class="health-report-card">
  <div class="hrc-header">
    <span class="hrc-icon">📋</span>
    <div>
      <div class="hrc-title">Document Verification Result</div>
      <div class="hrc-sub">📄 Medical / Health Report</div>
    </div>
  </div>

  <div class="hrc-section-label">🔎 Identity Check</div>
  <table class="hrc-table">
    ${matchRow('Name Match',   ic.name)}
    ${matchRow('Age Match',    ic.age)}
    ${matchRow('Gender Match', ic.gender)}
  </table>

  ${(ic.age||'').toUpperCase()==='NO' && d.reply
    ? `<div class="hrc-warning">⚠️ ${escHtml(d.reply.match(/age[^.]+\./i)?.[0]||'Age mismatch detected.')}</div>`
    : ''}

  <div class="hrc-section-label">🩺 Health Analysis</div>
  <table class="hrc-table">
    <tr>
      <td class="hrc-lbl">Conditions</td>
      <td class="hrc-val">${conds.length
        ? `<span style="color:#dc2626;font-weight:600">${conds.join(', ')}</span>`
        : '<span style="color:#16a34a;font-weight:600">None detected ✅</span>'}</td>
    </tr>
    <tr>
      <td class="hrc-lbl">Conclusion</td>
      <td class="hrc-val">${conds.length
        ? 'Conditions present — factored into recommendation'
        : 'All parameters are normal'}</td>
    </tr>
    ${d.doctor ? `<tr><td class="hrc-lbl">Doctor / Lab</td><td class="hrc-val">${escHtml(d.doctor)}</td></tr>` : ''}
  </table>

  <div class="hrc-risk-row" style="background:${riskColor[risk]||riskColor.LOW}20;border:1.5px solid ${riskColor[risk]||riskColor.LOW};">
    <span>📊 Insurance Risk Level</span>
    <span class="hrc-risk-badge" style="background:${riskColor[risk]||riskColor.LOW}">
      ${riskEmoji[risk]||'🟢'} ${risk}
    </span>
  </div>
</div>`;

    // Inject card as raw HTML bot bubble — using unified addBotBubble with rawHtml=true
    addBotBubble(cardHtml, [], 'none', true);

    if (vsresult) {
      vsresult.className = 'vsb-result success';
      vsresult.textContent = `✅ ${risk} Risk — ${conds.length ? conds.join(', ') : 'No conditions'}`;
    }
    showToast('✅ Health report analyzed!', 'success');

    // ── Dynamic routing — use next_stage from backend (not hardcoded) ──
    const nextSt   = d.next_stage || 'collect_budget';
    const nextOpts = d.options     || ['Under ₹500','₹500–₹1,000','₹1,000–₹2,000','₹2,000–₹5,000','Above ₹5,000'];
    const nextType = d.option_type || 'radio';
    const nextProg = { collect_budget:65, condition_report_upload:57, optional_health_check:57,
                       vehicle_history:57, life_docs:57, travel_declare:57, property_history:57 };
    const nextLbl  = { collect_budget:'Budget', condition_report_upload:'Health Report',
                       optional_health_check:'Health Check', vehicle_history:'Vehicle History',
                       life_docs:'Life Documents', travel_declare:'Travel Declare',
                       property_history:'Property History' };

    // Show upload card if next stage needs it
    const uploadNeeded = ['condition_report_upload','optional_health_check','life_docs'].includes(nextSt);

    setTimeout(() => {
      // Card already shown via addBotBubble(rawHtml) above
      // Step 1: Ask user if they want to upload another report OR continue
      // Only show budget AFTER user confirms they want to continue
      if (nextSt === 'collect_budget') {
        // Show confirm prompt first — not budget directly
        addBotBubble(
          `✅ Report analyzed! 🎉 Would you like to re-upload, or continue to the next step?`,
          ['🔄 Re-upload Report', '➡️ Continue'],
          'radio'
        );
        // Stage stays as collect_budget in DB already — when user clicks Continue
        // the chat handler will see collect_budget stage and show budget options
        // Tag the current stage so option handler knows context
        currentStage = 'health_report_confirm';
        updateProgress(nextProg[nextSt] || 65, nextLbl[nextSt] || 'Next Step', nextSt);
        // Award XP for uploading health report
        document.dispatchEvent(new CustomEvent('policybot:stage', {detail:{stage:'optional_medical_report', userId}}));
      } else {
        const nextMsg = d.reply || `Let's continue 👍`;
        addBotBubble(nextMsg, nextOpts, nextType);
        updateProgress(nextProg[nextSt] || 65, nextLbl[nextSt] || 'Next Step', nextSt);
        currentStage = nextSt;
        document.dispatchEvent(new CustomEvent('policybot:stage', {detail:{stage: nextSt, userId}}));
      }
      autoSelectDocType(nextSt);
      if (uploadNeeded) {
        document.getElementById('upload-card')?.classList.remove('hidden');
        document.getElementById('upload-section-wrap')?.classList.remove('hidden');
        document.getElementById('upload-drop-zone')?.classList.add('attention');
        setTimeout(() => document.getElementById('upload-section-wrap')?.scrollIntoView({ behavior:'smooth', block:'nearest' }), 200);
      } else {
        document.getElementById('upload-card')?.classList.add('hidden');
        document.getElementById('upload-section-wrap')?.classList.add('hidden');
      }
      setButtonsEnabled(true);
    }, 1200);
    return;
  }

  if (d.verified) {
    // ── Gov ID verified → show confirmed details, go to city ──────────
    setBadge('vb-govid', 'verified', 'Verified ✅');
    document.getElementById('id-verified-dot')?.classList.remove('hidden');
    document.getElementById('verif-row')?.classList.remove('hidden');
    showToast('✅ Identity verified! Details confirmed 👍', 'success');
    if (vsresult) { vsresult.className = 'vsb-result success'; vsresult.textContent = '✅ Identity Verified'; }

    // Get current profile to show confirmed details bubble (no gender question)
    fetch(`/api/profile?user_id=${encodeURIComponent(userId)}`)
      .then(r => r.json())
      .then(pd => {
        const p = pd.profile || {};
        const name   = p.name   || '';
        const age    = p.age    || '';
        const gender = p.gender || '';  // auto-extracted from ID — just show, don't ask
        const docTypeFound = d.doc_type_found || 'your ID';
        // Build confirmation block — gender shown if extracted, no radio needed for it
        const nameRow   = name   ? `👤 **Name:** ${name} ✅`   : '';
        const ageRow    = age    ? `🎂 **Age:** ${age} years ✅`  : '';
        const genderRow = gender ? `⚧ **Gender:** ${gender} ✅` : '';
        const detailBlock = [nameRow, ageRow, genderRow].filter(Boolean).join('\n');

        // next_stage is always collect_city — gender extracted from ID, never asked manually
        const nextSt = d.next_stage || 'collect_city';

        setTimeout(() => {
          addBotBubble(
            `✅ ${docTypeFound} verified successfully! 🎉\n\n` +
            (detailBlock ? `Details confirmed:\n${detailBlock}\n\n` : '') +
            `Which city do you live in? 🏙️`,
            [], 'none'
          );
          updateProgress(37, 'Your City', 'collect_city');
          currentStage = 'collect_city';
          // ── Award XP for document upload + id_verified badge ──────────────
          document.dispatchEvent(new CustomEvent('policybot:stage', {detail:{stage:'doc_upload', userId}}));
          document.getElementById('upload-card')?.classList.add('hidden');
      document.getElementById('upload-section-wrap')?.classList.add('hidden');
          setButtonsEnabled(true);
        }, 900);
      })
      .catch(() => {
        setTimeout(() => {
          // Fallback — no gender question, go straight to city
          document.dispatchEvent(new CustomEvent('policybot:stage', {detail:{stage:'doc_upload', userId}}));
          addBotBubble(
            "✅ ID verified! Which city do you live in? 🏙️",
            [], 'none'
          );
          updateProgress(37, 'Your City', 'collect_city');
          currentStage = 'collect_city';
          document.getElementById('upload-card')?.classList.add('hidden');
      document.getElementById('upload-section-wrap')?.classList.add('hidden');
          setButtonsEnabled(true);
        }, 900);
      });

  } else {
    // ── Verification failed ───────────────────────────────────────────────
    const icon    = d.v_status === 'api_error' ? '⚠️' : '❌';
    const msgType = d.v_status === 'api_error' ? 'warn' : 'fail';
    setBadge('vb-govid', 'failed', 'Not Verified');
    // Show failure message as bot bubble (not system message to avoid overlap)
    addBotBubble(d.reply || 'Verification failed. Please try a different document.');
    document.getElementById('verif-row')?.classList.remove('hidden');
    if (vsresult) {
      vsresult.className = `vsb-result ${msgType === 'warn' ? 'warn' : 'fail'}`;
      vsresult.textContent = d.reply || 'Not verified';
    }

    // Quality tips
    const tips = {
      blurry:     '📸 Tip: Hold your phone steady and tap to focus.',
      dark:       '💡 Tip: Move to a brighter area or use flashlight.',
      cropped:    '📐 Tip: Make sure all 4 corners are visible.',
      incomplete: '📋 Tip: Ensure the complete document is visible.',
      unreadable: '🔍 Tip: Place flat and photograph from directly above.',
    };
    if (d.quality && tips[d.quality]) {
      setTimeout(() => addBotBubble(tips[d.quality]), 400);
    }

    const fallbackOpts = isConditionDoc
      ? ['Upload Again', 'Skip — Continue Without']
      : ['Upload Document Again', 'Continue Without Verification'];

    setTimeout(() => {
      renderOptions(d.options?.length ? d.options : fallbackOpts,
                    d.option_type || 'radio');
      setButtonsEnabled(true);
    }, 800);
  }
}

// ─── Verification Overlay ─────────────────────────────────────────────────────
function showVerifOverlay(docType) {
  document.getElementById('verif-overlay')?.classList.remove('hidden');

  const lbl = document.getElementById('engine-label');
  if (lbl) lbl.textContent = 'Offline OCR Active';

  const isCondition = ['health_report','condition_report','vehicle_insurance',
    'rc_book','life_doc','travel_doc','property_doc'].includes(docType);

  const titleMap = {
    health_report:     'Analyzing Health Report',
    condition_report:  'Analyzing Medical Report',
    rc_book:           'Analyzing RC Book',
    vehicle_insurance: 'Analyzing Vehicle Document',
    life_doc:          'Analyzing Life Document',
    travel_doc:        'Analyzing Travel Document',
    property_doc:      'Analyzing Property Document',
    prev_policy:       'Analyzing Policy Document',
  };
  const titleEl = document.getElementById('verif-overlay-title');
  if (titleEl) titleEl.textContent = titleMap[docType] || 'Analyzing Document';

  const stepLabels = isCondition
    ? ['Document Received','Checking Quality','Extracting Text (OCR)','Identifying Conditions','Storing Result']
    : ['Document Received','Checking Image Quality','Extracting Name & Date of Birth','Verifying Name Match','Verifying Age Match'];

  for (let i = 1; i <= 5; i++) {
    const el = document.getElementById(`vos-${i}`);
    if (!el) continue;
    el.className = 'vos';
    const icon = el.querySelector('i');
    if (icon) {
      el.innerHTML = '';
      el.appendChild(icon.cloneNode(true));
      el.appendChild(document.createTextNode(' ' + stepLabels[i-1]));
    } else {
      el.textContent = stepLabels[i-1];
    }
  }
  setVerifStep(1);
  setVerifSub('Securely uploading your document…');
}

function setVerifStep(n) {
  [1,2,3,4,5].forEach(i => {
    const el = document.getElementById(`vos-${i}`);
    if (!el) return;
    if (i < n)       el.className = 'vos done';
    else if (i === n){ el.className = 'vos active'; }
    else             el.className = 'vos';
  });
  const subtitles = ['','Document received securely…','Checking image quality…',
    'Reading name & date of birth…','Matching name against profile…','Verifying age match…'];
  setVerifSub(subtitles[n] || 'Processing…');
}
function setVerifSub(txt) {
  const el = document.getElementById('verif-overlay-sub');
  if (el) el.textContent = txt;
}
function hideVerifOverlay() {
  document.getElementById('verif-overlay')?.classList.add('hidden');
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ─── Option Buttons (radio + multi) ──────────────────────────────────────────
function renderOptions(options, type) {
  const zone = document.getElementById('options-zone');
  if (!zone) return;
  zone.innerHTML = '';
  if (!options || !options.length) return;

  const zoneId = 'oz_' + Date.now();
  multiSelections[zoneId] = [];

  if (type === 'multi') {
    const lbl = document.createElement('div');
    lbl.className = 'opt-label';
    lbl.textContent = 'Select all that apply:';
    zone.appendChild(lbl);
  }

  options.forEach(opt => {
    const btn = document.createElement('button');
    btn.className = `opt-btn${type === 'multi' ? ' multi-btn' : ''}`;
    btn.innerHTML = `<span class="btn-text">${escHtml(opt)}</span>`;
    btn.dataset.opt  = opt;
    btn.dataset.type = type;
    btn.dataset.zone = zoneId;
    btn.disabled = buttonsDisabled;
    if (buttonsDisabled) btn.classList.add('disabled');
    btn.addEventListener('click', e => handleOption(e, btn, opt, type, zoneId));
    zone.appendChild(btn);
  });

  if (type === 'multi') {
    const conf = document.createElement('button');
    conf.className = 'multi-confirm-btn';
    conf.innerHTML = '<i class="fas fa-check"></i> Confirm Selection';
    conf.disabled = buttonsDisabled;
    conf.onclick = () => confirmMulti(zoneId);
    zone.appendChild(conf);
  }
}

function handleOption(e, btn, opt, type, zoneId) {
  if (btn.disabled || buttonsDisabled) return;
  addRipple(e, btn);

  if (type === 'radio') {
    document.querySelectorAll(`[data-zone="${zoneId}"]`).forEach(b =>
      b.classList.remove('selected-radio')
    );
    btn.classList.add('selected-radio');
    update3DPlanLabel(opt);
    // Highlight the selected plan node in 3D
    const planIdx = planNodes3D.indexOf(opt);
    if (planIdx >= 0 && typeof PolicyBot3D !== 'undefined') PolicyBot3D.highlightPlan(planIdx);

    // ── Special: optional medical report upload button ──────────────
    // ── health_report_confirm: after report analyzed, user chooses next action ──
  if (currentStage === 'health_report_confirm') {
    if (opt === '➡️ Continue') {
      currentStage = 'collect_budget';
      clearOptions();
      addBotBubble(
        `💰 Almost there! What is your preferred monthly budget for insurance?`,
        ['Under ₹500','₹500–₹1,000','₹1,000–₹2,000','₹2,000–₹5,000','Above ₹5,000'],
        'radio'
      );
      updateProgress(65, 'Budget', 'collect_budget');
      // Pure frontend — no doSend, no Gemini call
    } else if (opt === '🔄 Re-upload Report') {
      currentStage = 'optional_medical_report';
      clearOptions();
      addBotBubble(`Sure! Please upload another report below 📎`, [], 'none');
      document.getElementById('upload-card')?.classList.remove('hidden');
      document.getElementById('upload-section-wrap')?.classList.remove('hidden');
    }
    return;
  }

  if (currentStage === 'optional_medical_report' && opt === 'Upload medical report') {
      setTimeout(() => {
        clearOptions();
        // Show upload card and focus it — do NOT send to backend yet
        document.getElementById('upload-card')?.classList.remove('hidden');
        document.getElementById('upload-section-wrap')?.classList.remove('hidden');
        autoSelectDocType('optional_medical_report');
        document.getElementById('upload-drop-zone')?.classList.add('attention');
        addBotBubble(
          'Please upload your medical or health report using the upload widget on the left 📋 '
          + 'Accepted formats: JPG, PNG, PDF.',
          [], 'none'
        );
        setButtonsEnabled(true);
      }, 350);
      return;
    }

    setTimeout(() => { clearOptions(); doSend(opt, opt); }, 350);
  } else {
    btn.classList.toggle('selected-multi');
    const sel = multiSelections[zoneId] || [];
    const idx = sel.indexOf(opt);
    if (idx === -1) sel.push(opt); else sel.splice(idx, 1);
    multiSelections[zoneId] = sel;
  }
}

function confirmMulti(zoneId) {
  const sel = multiSelections[zoneId] || [];
  if (!sel.length) { showToast('Please select at least one option 😊', 'info'); return; }
  clearOptions();
  const combined = sel.join(', ');
  doSend(combined, combined);
}

function clearOptions(hideRating = false) {
  const zone = document.getElementById('options-zone');
  if (zone) zone.innerHTML = '';
  // Only hide rating panel when explicitly requested
  if (hideRating) document.getElementById('rating-panel')?.classList.add('hidden');
  document.getElementById('esc-panel')?.classList.add('hidden');
}

// ── 3D Visualizer controls ──────────────────────────────────────────────────
function show3DVisualizer(planOptions) {
  const card = document.getElementById('viz3d-card');
  if (!card) return;
  card.classList.remove('hidden');
  const insType = document.getElementById('pv-ins')?.textContent?.trim() || 'Health Insurance';
  if (typeof PolicyBot3D !== 'undefined') {
    PolicyBot3D.init('viz3d-canvas', insType, planOptions || []);
  }
  const lbl = document.getElementById('viz3d-plan-name');
  if (lbl) lbl.textContent = planOptions.length ? `${planOptions.length} plans available` : 'Select a plan to view';
}

function hide3DVisualizer() {
  const card = document.getElementById('viz3d-card');
  if (card) card.classList.add('hidden');
  if (typeof PolicyBot3D !== 'undefined') PolicyBot3D.destroy();
}

function update3DPlanLabel(planName) {
  const lbl = document.getElementById('viz3d-plan-name');
  if (lbl) lbl.textContent = planName || '';
}

// ── Session quick-action bar ───────────────────────────────────────────────
function showSessionQuickBar() {
  if (document.getElementById('session-quick-bar')) return; // already shown
  const zone = document.getElementById('options-zone');
  if (!zone) return;
  const bar = document.createElement('div');
  bar.id = 'session-quick-bar';
  bar.className = 'session-quick-bar';
  bar.innerHTML = `
    <button class="sqb-btn sqb-btn-rate" onclick="quickRate()">
      <i class="fas fa-star"></i> Rate & End
    </button>
    <button class="sqb-btn sqb-btn-end" onclick="quickEnd()">
      <i class="fas fa-times-circle"></i> Wind Up
    </button>`;
  // Insert before options zone content
  zone.insertBefore(bar, zone.firstChild);
}

function hideSessionQuickBar() {
  const bar = document.getElementById('session-quick-bar');
  if (bar) bar.remove();
}

function quickRate() {
  hideSessionQuickBar();
  hide3DVisualizer();
  doSend('wind up', 'Wind Up Session');
}

function quickEnd() {
  hideSessionQuickBar();
  hide3DVisualizer();
  doSend('wind up', 'Wind Up Session');
}

function addRipple(e, btn) {
  const r = document.createElement('span');
  r.className = 'ripple';
  const rect = btn.getBoundingClientRect();
  const size = Math.max(rect.width, rect.height);
  r.style.cssText = `width:${size}px;height:${size}px;`
    + `left:${e.clientX - rect.left - size/2}px;`
    + `top:${e.clientY - rect.top - size/2}px`;
  btn.appendChild(r);
  setTimeout(() => r.remove(), 600);
}

// ─── Message rendering ────────────────────────────────────────────────────────
function addBotBubble(text, options=[], optType='none', rawHtml=false) {
  const c = document.getElementById('chat-messages');
  if (!c) return;
  const row = document.createElement('div');
  row.className = 'msg-row bot';
  const bubbleContent = rawHtml ? text : fmtMsg(text);
  row.innerHTML = `
    <div class="msg-av"><i class="fas fa-robot"></i></div>
    <div class="msg-inner">
      <div class="msg-bubble-bot"${rawHtml ? ' style="padding:0;overflow:hidden;"' : ''}>${bubbleContent}</div>
      <div class="msg-time">${nowStr()}</div>
    </div>`;
  c.appendChild(row);
  // Ensure message is visible — scroll into view
  row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  c.scrollTop = c.scrollHeight;
  // Always clear stale options first, then render new ones if any
  const zone = document.getElementById('options-zone');
  if (zone && !options.length) zone.innerHTML = '';
  if (options.length) renderOptions(options, optType);
  // Speak bot reply if TTS enabled (skip raw HTML blobs)
  if (!rawHtml) speakText(text);
}

function addUserBubble(text) {
  const c = document.getElementById('chat-messages');
  if (!c) return;
  const row = document.createElement('div');
  row.className = 'msg-row user';
  row.innerHTML = `
    <div class="msg-av"><i class="fas fa-user"></i></div>
    <div class="msg-inner">
      <div class="msg-bubble-user">${escHtml(text)}</div>
      <div class="msg-time">${nowStr()}</div>
    </div>`;
  c.appendChild(row);
  c.scrollTop = c.scrollHeight;
}

function addSystemMsg(text, type='info') {
  const c = document.getElementById('chat-messages');
  if (!c) return;
  const row = document.createElement('div');
  row.className = 'msg-row system';
  row.innerHTML = `<div class="msg-bubble-system ${type}">${escHtml(text)}</div>`;
  c.appendChild(row);
  c.scrollTop = c.scrollHeight;
}

function fmtMsg(text) {
  let t = escHtml(text);
  t = t.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/\n/g, '<br>');
  return t;
}
// ─── Smart Typing Suggestions ────────────────────────────────────────────────
// Suggestion chips removed
function renderSuggestions(stage) { /* disabled */ }

function escHtml(t) {
  return (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function nowStr() {
  return new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

// ─── Typing indicator ─────────────────────────────────────────────────────────
function showTyping(show) {
  document.getElementById('typing-row')?.classList.toggle('hidden', !show);
  if (show) {
    const c = document.getElementById('chat-messages');
    if (c) c.scrollTop = c.scrollHeight;
  }
}

// ─── Rating ───────────────────────────────────────────────────────────────────
function showRatingPanel() {
  const panel = document.getElementById('rating-panel');
  if (panel) {
    panel.classList.remove('hidden');
    // Reset stars and button state
    selectedStar = 0;
    document.querySelectorAll('.star-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('rating-submit')?.classList.add('hidden');
  }
}
function setStar(n) {
  selectedStar = n;
  document.querySelectorAll('.star-btn').forEach((b,i) =>
    b.classList.toggle('active', i < n)
  );
  document.getElementById('rating-submit')?.classList.remove('hidden');
}
async function submitRating() {
  if (!selectedStar) { showToast('Please tap a star to rate 😊', 'info'); return; }

  // ── Show thank-you animation inside rating panel ───────────────────────
  const panel = document.getElementById('rating-panel');
  const starsStr = '⭐'.repeat(selectedStar);
  const msgs = ['Thanks! 🙏','Appreciated! 😊','You are awesome! 🎉','Wonderful! 🌟','Amazing! 🏆'];
  const thankMsg = msgs[Math.min(selectedStar - 1, msgs.length - 1)];

  if (panel) {
    panel.innerHTML = `
      <div class="rating-thankyou">
        <div class="rty-stars">${starsStr}</div>
        <div class="rty-msg">${thankMsg}</div>
        <div class="rty-sub">Your feedback means a lot to us!</div>
      </div>`;
  }

  try {
    // Store rating via API
    await fetch('/api/rating', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ user_id: userId, rating: selectedStar })
    });

    // Wait for user to see the animation, then trigger farewell
    await new Promise(r => setTimeout(r, 1800));

    // Add farewell bot message
    const farewellMsg = `🎉 Thank you for the ${selectedStar}-star rating! It means a lot to us. Wishing you a safe and secure future! 😊`;
    addBotBubble(farewellMsg);

    // Hide rating panel and trigger farewell overlay
    setTimeout(() => {
      if (panel) panel.classList.add('hidden');
      showFarewell(farewellMsg);
      triggerCleanup();
      lockChatInput();
    }, 1200);

  } catch(e) {
    showToast('Could not submit rating', 'error');
    if (panel) panel.classList.add('hidden');
  }
}

// ─── Escalation ───────────────────────────────────────────────────────────────
async function submitEscalation() {
  const phone = document.getElementById('esc-phone')?.value.trim();
  const time  = document.getElementById('esc-time')?.value.trim();
  if (!phone) { showToast('Please enter your phone number 😊', 'info'); return; }
  try {
    const res = await fetch('/api/escalate', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ user_id: userId, phone, best_time: time })
    });
    const d = await res.json();
    document.getElementById('esc-panel')?.classList.add('hidden');
    addBotBubble(d.reply || '✅ Human advisor notified! They will call you soon 📞');
    showToast('Callback requested!', 'success');
  } catch(e) { showToast('Could not submit request', 'error'); }
}

// ─── Farewell + cleanup ───────────────────────────────────────────────────────
function showFarewell(text) {
  const el = document.getElementById('farewell-text');
  if (el) el.textContent = text || '🎉 Thank you! Wishing you a safe and secure future.';
  document.getElementById('farewell-overlay')?.classList.remove('hidden');
  spawnConfetti();

  // Show download report button
  const box = document.querySelector('.farewell-box');
  if (box && !document.getElementById('dl-report-btn')) {
    const btn = document.createElement('button');
    btn.id = 'dl-report-btn';
    btn.className = 'farewell-btn dl-report-btn';
    btn.innerHTML = '📄 Download Analysis Report (PDF)';
    btn.onclick = downloadAnalysisReport;
    // Insert before "Start New Chat"
    const existing = box.querySelector('.farewell-btn');
    if (existing) box.insertBefore(btn, existing);
    else box.appendChild(btn);
  }
}
function startNewChat() {
  document.getElementById('farewell-overlay')?.classList.add('hidden');
  // Generate completely new IDs — guarantees a fresh DB row on backend
  userId        = genId();
  sessionId     = genId();
  isFirstMessage = true;

  // If landing screen exists, restore it for a fresh session
  const landing = document.getElementById('landing-screen');
  const shell   = document.getElementById('floating-shell');
  if (landing && shell) {
    // Reset landing
    landing.style.display = '';
    landing.classList.remove('hiding');
    shell.classList.add('hidden');
    shell.classList.remove('chat-launching');
    // Clear chat messages
    document.getElementById('chat-messages').innerHTML = '';
    document.getElementById('options-zone').innerHTML  = '';
  } else {
    location.reload();
  }
}
async function triggerCleanup() {
  try {
    await fetch('/api/cleanup', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ user_id: userId })
    });
  } catch(e) {}
}
// ─── Download Analysis Report (PDF) ──────────────────────────────────────────
// ─── Plan Comparison Table ────────────────────────────────────────────────────
function injectPlanCompareTable(plans) {
  const chatBox = document.getElementById('chat-messages');
  if (!chatBox || !plans || !plans.length) return;

  const medals = ['🥇', '🥈', '🥉'];
  const cols = plans.length;

  // Build header cells
  let headerCells = '<th class="pct-label">Feature</th>';
  plans.forEach((p, i) => {
    headerCells += `
      <th class="pct-plan">
        <div class="pct-medal">${medals[i] || '✅'}</div>
        <div class="pct-name">${escHtml(p.name)}</div>
        <div class="pct-company">${escHtml(p.company)}</div>
      </th>`;
  });

  // Build data rows
  const rows = [
    { label: '💰 Premium',       key: 'premium'   },
    { label: '🛡️ Coverage',      key: 'coverage'  },
    { label: '⏱️ Waiting Period', key: 'waiting'   },
    { label: '👤 Eligible Age',   key: 'age'       },
    { label: '⭐ Key Benefit',    key: 'benefits'  },
    { label: '✅ Why This Plan',  key: 'reason'    },
  ];

  let bodyRows = '';
  rows.forEach(r => {
    bodyRows += `<tr><td class="pct-label">${r.label}</td>`;
    plans.forEach(p => {
      const val = p[r.key] || '—';
      bodyRows += `<td class="pct-cell">${escHtml(val)}</td>`;
    });
    bodyRows += '</tr>';
  });

  const tableHtml = `
    <div class="plan-compare-wrap" id="plan-compare-table">
      <div class="pct-header">
        <i class="fas fa-table-columns"></i> Plan Comparison
        <span class="pct-badge">${cols} Plans</span>
      </div>
      <div class="pct-scroll">
        <table class="pct-table">
          <thead><tr>${headerCells}</tr></thead>
          <tbody>${bodyRows}</tbody>
        </table>
      </div>
      <div class="pct-footer">
        Tap a plan name above to select it · Powered by PolicyBot AI
      </div>
    </div>`;

  const row = document.createElement('div');
  row.className = 'msg-row bot pct-row';
  row.innerHTML = `
    <div class="msg-av"><i class="fas fa-robot"></i></div>
    <div class="msg-inner" style="max-width:100%;width:100%">
      <div class="msg-bubble-bot" style="padding:0;overflow:hidden;border-radius:14px;">${tableHtml}</div>
      <div class="msg-time">${nowStr()}</div>
    </div>`;
  chatBox.appendChild(row);
  chatBox.scrollTop = chatBox.scrollHeight;
}

// ─── Report Action Buttons (Download + WhatsApp) ──────────────────────────────
function injectReportButtons() {
  const chatBox = document.getElementById('chat-messages');
  if (!chatBox) return;

  const wrap = document.createElement('div');
  wrap.id = 'inline-report-btn-wrap';
  wrap.className = 'report-btn-wrap';
  wrap.innerHTML = `
    <button class="rpt-btn rpt-dl" id="inline-report-btn" onclick="downloadAnalysisReport()">
      <i class="fas fa-file-pdf"></i> Download PDF Report
    </button>`;
  chatBox.appendChild(wrap);
  chatBox.scrollTop = chatBox.scrollHeight;
}

// WhatsApp feature removed


async function downloadAnalysisReport() {
  const btn = document.getElementById('inline-report-btn') || document.getElementById('dl-report-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generating…'; }

  try {
    if (!userId) throw new Error('Session not found — please refresh and try again');

    const res = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId })
    });

    // Check content-type BEFORE calling blob() — a JSON error body must be read as text
    const ct = res.headers.get('content-type') || '';
    if (!res.ok || !ct.includes('pdf')) {
      let errMsg = `Server error (HTTP ${res.status})`;
      try {
        const errData = await res.json();
        errMsg = errData.error || errData.message || errMsg;
      } catch(_) {
        try { errMsg = await res.text(); } catch(__) {}
      }
      throw new Error(errMsg);
    }

    const blob = await res.blob();
    if (!blob || blob.size < 500) throw new Error('Generated PDF appears empty — please try again');

    const url = URL.createObjectURL(blob);
    const a   = document.createElement('a');
    a.href     = url;
    a.download = `PolicyBot_Report_${Date.now()}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showToast('✅ Report downloaded successfully!', 'success');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-file-pdf"></i> Download PDF Report'; }
  } catch(e) {
    console.error('[Report error]', e);
    showToast(`❌ Report error: ${e.message || 'Please try again.'}`, 'error');
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-file-pdf"></i> Download PDF Report'; }
  }
}


// ─── Confetti ─────────────────────────────────────────────────────────────────
function spawnConfetti() {
  const container = document.getElementById('farewell-confetti');
  if (!container) return;
  const colors = ['#5a72ff','#9d4edd','#f59e0b','#10b981','#ef4444','#22d3ee'];
  for (let i = 0; i < 55; i++) {
    const dot = document.createElement('div');
    const size = Math.random() * 8 + 4;
    dot.style.cssText = `position:absolute;width:${size}px;height:${size}px;`
      + `background:${colors[Math.floor(Math.random()*colors.length)]};`
      + `border-radius:${Math.random()>.5?'50%':'3px'};`
      + `left:${Math.random()*100}%;top:${Math.random()*100}%;`
      + `animation:confettiFall ${1.2+Math.random()*1.8}s ease-in ${Math.random()*.8}s forwards;`
      + `opacity:0;`;
    container.appendChild(dot);
  }
  const style = document.createElement('style');
  style.textContent = `@keyframes confettiFall{0%{opacity:1;transform:translateY(-20px) rotate(0)}100%{opacity:0;transform:translateY(120px) rotate(360deg)}}`;
  document.head.appendChild(style);
}

// ─── Confidence ───────────────────────────────────────────────────────────────
function showConfidence(text) {
  const card = document.getElementById('confidence-card');
  const val  = document.getElementById('conf-value');
  if (card && val) {
    val.textContent = text;
    card.classList.remove('hidden');
  }
}

// ─── Progress bar + dots ──────────────────────────────────────────────────────
function updateProgress(pct, label, stage) {
  const p = pct || 7;
  const fill = document.getElementById('prog-fill');
  const pctEl = document.getElementById('prog-pct');
  const lblEl = document.getElementById('prog-stage-label');
  if (fill) fill.style.width = p + '%';
  if (pctEl) pctEl.textContent = p + '%';
  if (lblEl && label) lblEl.textContent = label;

  const dots = document.querySelectorAll('.pd');
  const activeIdx = DOT_STAGES.indexOf(stage);
  dots.forEach((d, i) => {
    d.classList.remove('active','done');
    if (i < activeIdx)      d.classList.add('done');
    else if (i === activeIdx) d.classList.add('active');
  });
}

// ─── Profile sidebar refresh ──────────────────────────────────────────────────
function refreshProfile() {
  fetch(`/api/profile?user_id=${encodeURIComponent(userId)}`)
    .then(r => r.json())
    .then(d => {
      const p = d.profile || {};
      if (p.name) {
        const pName = document.getElementById('p-name');
        if (pName) pName.textContent = p.name;
      }
      setField('pf-ins',    'pv-ins',    p.insurance_type);
      setField('pf-age',    'pv-age',    p.age ? `${p.age} yrs` : null);
      setField('pf-city',   'pv-city',   p.city);
      setField('pf-budget', 'pv-budget', p.budget_range);
      setField('pf-medical','pv-medical',p.medical_conditions);
      if (p.gov_id_verified) {
        setBadge('vb-govid', 'verified', 'Verified ✅');
        document.getElementById('id-verified-dot')?.classList.remove('hidden');
        document.getElementById('verif-row')?.classList.remove('hidden');
      }
      // ── Show risk score + premium prediction after analysis completes ──
      if (p.risk_score && parseInt(p.risk_score) > 0) {
        setField('pf-risk', 'pv-risk',
          `${p.risk_score}/100 — ${p.risk_category || ''}`);
      }
      if (p.premium_prediction) {
        setField('pf-premium', 'pv-premium', p.premium_prediction);
      }
      // Update claim probability gauge if risk has been scored
      if (p.risk_score && parseInt(p.risk_score) > 0 && p.claim_probability) {
        updateClaimGauge(parseInt(p.claim_probability));
      } else if (p.risk_score && parseInt(p.risk_score) > 0) {
        // Fetch claim score on-demand
        fetch('/api/claim-score', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({user_id: userId})
        }).then(r=>r.json()).then(d=>{
          if (d.claim_probability) updateClaimGauge(d.claim_probability);
        }).catch(()=>{});
      }
    }).catch(() => {});
}

function setField(rowId, valId, val) {
  if (val) {
    document.getElementById(rowId)?.classList.remove('hidden');
    const el = document.getElementById(valId);
    if (el) el.textContent = val;
  }
}
function setBadge(id, cls, label) {
  const b = document.getElementById(id);
  if (b) { b.className = `verif-badge ${cls}`; b.textContent = label; }
}

// ─── Voice ────────────────────────────────────────────────────────────────────
function initVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    const micBtn = document.getElementById('mic-btn');
    if (micBtn) micBtn.style.display = 'none';
    return;
  }
  recognition = new SR();
  recognition.continuous    = false;
  recognition.interimResults = true;
  recognition.onresult = e => {
    const inp = document.getElementById('chat-input');
    if (inp) inp.value = Array.from(e.results).map(r => r[0].transcript).join('');
  };
  recognition.onend = () => {
    const txt = document.getElementById('chat-input')?.value.trim();
    stopVoice();
    if (txt) setTimeout(() => sendMsg(), 200);
  };
  recognition.onerror = () => stopVoice();
}
function toggleVoice() { isListening ? stopVoice() : startVoice(); }
function startVoice() {
  if (!recognition) return;
  isListening = true;
  document.getElementById('mic-btn')?.classList.add('listening');
  document.getElementById('waveform-bar')?.classList.remove('hidden');
  recognition.lang = getLangCode(currentLang);
  recognition.start();
  showToast('🎤 Listening…', 'info');
}
function stopVoice() {
  isListening = false;
  document.getElementById('mic-btn')?.classList.remove('listening');
  document.getElementById('waveform-bar')?.classList.add('hidden');
  if (recognition) try { recognition.stop(); } catch(e) {}
}
function getLangCode(l) {
  return l === 'Tamil' ? 'ta-IN' : l === 'Hindi' ? 'hi-IN' : 'en-IN';
}

// ─── Text-to-Speech (TTS) ────────────────────────────────────────────────────
function initTTS() {
  if (!window.speechSynthesis) return;
  // Inject TTS toggle button into topbar-right if not already there
  const topRight = document.querySelector('.gpt-topbar-right');
  if (!topRight || document.getElementById('tts-toggle-btn')) return;
  const btn = document.createElement('button');
  btn.id = 'tts-toggle-btn';
  btn.className = 'icon-btn tts-btn';
  btn.title = 'Voice replies (TTS)';
  btn.innerHTML = '<i class="fas fa-volume-xmark" id="tts-icon"></i>';
  btn.onclick = toggleTTS;
  // Insert before the first existing button in topbar-right
  topRight.insertBefore(btn, topRight.firstChild);
}

function toggleTTS() {
  ttsEnabled = !ttsEnabled;
  const icon = document.getElementById('tts-icon');
  const btn  = document.getElementById('tts-toggle-btn');
  if (ttsEnabled) {
    if (icon) icon.className = 'fas fa-volume-high';
    if (btn)  btn.classList.add('tts-active');
    showToast('🔊 Voice replies ON', 'info');
  } else {
    if (icon) icon.className = 'fas fa-volume-xmark';
    if (btn)  btn.classList.remove('tts-active');
    window.speechSynthesis.cancel();
    showToast('🔇 Voice replies OFF', 'info');
  }
}

function speakText(text) {
  if (!ttsEnabled || !window.speechSynthesis) return;
  // Strip markdown, emojis, and excessive punctuation
  const clean = text
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/[🥇🥈🥉🎉✅❌⚠️📋💰🛡️⏱️👤⭐🔍📄🤖]/gu, '')
    .replace(/\n+/g, '. ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 500);   // cap at 500 chars — avoid very long speeches
  if (!clean) return;
  window.speechSynthesis.cancel();
  ttsUtterance = new SpeechSynthesisUtterance(clean);
  ttsUtterance.lang  = getLangCode(currentLang);
  ttsUtterance.rate  = 0.95;
  ttsUtterance.pitch = 1.0;
  // Pick a natural voice if available
  const voices = window.speechSynthesis.getVoices();
  const langCode = getLangCode(currentLang).split('-')[0];
  const preferred = voices.find(v => v.lang.startsWith(langCode) && !v.name.includes('Google'));
  if (preferred) ttsUtterance.voice = preferred;
  window.speechSynthesis.speak(ttsUtterance);
}

// ─── Input helpers ────────────────────────────────────────────────────────────
function onKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
}
function autoH(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 110) + 'px';
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg, type='info') {
  const wrap = document.getElementById('toast-wrap');
  if (!wrap) return;
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  const icons = {success:'✅', error:'❌', info:'ℹ️', warn:'⚠️'};
  t.innerHTML = `<span>${icons[type]||'ℹ️'}</span><span>${escHtml(msg)}</span>`;
  wrap.appendChild(t);
  setTimeout(() => {
    t.style.opacity   = '0';
    t.style.transform = 'translateX(30px)';
    setTimeout(() => t.remove(), 300);
  }, 3500);
}

// ─── Particles ────────────────────────────────────────────────────────────────
function initParticles() {
  const canvas = document.getElementById('particle-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W = canvas.width  = innerWidth;
  let H = canvas.height = innerHeight;
  const pts = Array.from({length:50}, () => ({
    x: Math.random()*W, y: Math.random()*H,
    vx: (Math.random()-.5)*.4, vy: (Math.random()-.5)*.4,
    r: Math.random()*1.4+.4,   o: Math.random()*.3+.1
  }));
  function draw() {
    ctx.clearRect(0,0,W,H);
    const body = document.getElementById('body-root');
    const cls  = body?.className || '';
    if (cls.includes('clean') || cls.includes('minimal')) {
      requestAnimationFrame(draw); return;
    }
    pts.forEach(p => {
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0) p.x = W; if (p.x > W) p.x = 0;
      if (p.y < 0) p.y = H; if (p.y > H) p.y = 0;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI*2);
      ctx.fillStyle = `rgba(90,114,255,${p.o})`;
      ctx.fill();
    });
    requestAnimationFrame(draw);
  }
  draw();
  window.addEventListener('resize', () => {
    W = canvas.width  = innerWidth;
    H = canvas.height = innerHeight;
  });
}

// ─── Conversation Memory Widget ───────────────────────────────────────────────
const MEM_STEP_IDS = [
  'data_extraction','government_id_verification','location_confirmation',
  'coverage_selection','medical_condition_check','budget_collection',
  'review_details','fraud_detection','recommendation_generation'
];

// Map engine stage → memory step that should be "active"
const STAGE_TO_MEM_STEP = {
  insurance_type:'data_extraction', collect_name:'data_extraction',
  collect_age:'data_extraction',
  doc_upload:'government_id_verification', verify_wait:'government_id_verification',
  collect_gender:'location_confirmation', collect_city:'location_confirmation',
  collect_coverage:'coverage_selection',
  collect_family_count:'coverage_selection', collect_family_medical:'coverage_selection',
  collect_medical_status:'medical_condition_check', collect_medical:'medical_condition_check',
  optional_medical_report:'medical_condition_check',
  collect_budget:'budget_collection',
  review_details:'review_details', edit_details:'review_details',
  fraud_check:'fraud_detection', risk_scoring:'fraud_detection',
  recommendation:'recommendation_generation', explain_plan:'recommendation_generation',
  ask_escalation:'recommendation_generation', ask_rating:'recommendation_generation',
  farewell:'recommendation_generation',
};

let _memLastStage = '';

function updateMemoryWidget(stage, completedSteps) {
  if (!stage) return;
  const activeStep = STAGE_TO_MEM_STEP[stage] || '';

  MEM_STEP_IDS.forEach(stepId => {
    const el = document.getElementById('mst-' + stepId);
    if (!el) return;
    el.classList.remove('pending','active','done');
    if (completedSteps && completedSteps.includes(stepId)) {
      el.classList.add('done');
    } else if (stepId === activeStep) {
      el.classList.add('active');
    } else {
      el.classList.add('pending');
    }
  });
}

async function fetchMemory() {
  const uid = window._userId || '';
  if (!uid) return;
  try {
    const r = await fetch('/api/memory', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({user_id: uid})
    });
    if (!r.ok) return;
    const d = await r.json();
    updateMemoryWidget(d.conversation_state, d.completed_steps || []);
  } catch(e) { /* silent */ }
}

// Poll memory every 4 seconds
setInterval(fetchMemory, 4000);

// Also update immediately when we get a chat response
const _origAddBotBubble = window.addBotBubble;
if (typeof addBotBubble === 'function') {
  // Hook into the existing doSend success path
  document.addEventListener('policybot:stage', (e) => {
    const stage = e.detail?.stage || '';
    if (stage && stage !== _memLastStage) {
      _memLastStage = stage;
      // Mark step based on stage immediately without waiting for poll
      const completedEl = [];
      MEM_STEP_IDS.forEach(s => {
        const el = document.getElementById('mst-' + s);
        if (el && el.classList.contains('done')) completedEl.push(s);
      });
      updateMemoryWidget(stage, completedEl);
      // Trigger full refresh
      setTimeout(fetchMemory, 500);
    }
  });
}


// ════════════════════════════════════════════════════════════════════
// PREMIUM CALCULATOR
// ════════════════════════════════════════════════════════════════════
let calcDebounce = null;

function openCalc() {
  document.getElementById('calc-overlay')?.classList.remove('hidden');
  // Show family slider for non-individual coverages
  calcPreview();
}
function closeCalc() {
  document.getElementById('calc-overlay')?.classList.add('hidden');
}

// Live preview — debounced 400ms so sliders feel smooth
document.addEventListener('change', e => {
  if (e.target.id === 'ci-ins' || e.target.id === 'ci-age') calcPreview();
});
document.addEventListener('input', e => {
  if (e.target.id === 'ci-age') calcPreview();
});

function calcPreview() {
  clearTimeout(calcDebounce);
  calcDebounce = setTimeout(_doCalcFetch, 400);

  // Toggle family slider visibility
  const cov = document.getElementById('ci-cov')?.value || '';
  const famField = document.getElementById('ci-fam-field');
  if (famField) famField.style.display = cov.includes('Myself only') ? 'none' : '';
}

async function _doCalcFetch() {
  const ins     = document.getElementById('ci-ins')?.value  || 'health';
  const age     = parseInt(document.getElementById('ci-age')?.value || 30);
  const cov     = document.getElementById('ci-cov')?.value  || 'Myself only';
  const fam     = parseInt(document.getElementById('ci-fam')?.value || 3);
  const med     = document.getElementById('ci-med')?.value  || 'None';

  const resultEl = document.getElementById('calc-result');
  if (!resultEl) return;
  resultEl.innerHTML = '<div class="calc-loading"><i class="fas fa-spinner fa-spin"></i> Calculating…</div>';

  try {
    const res = await fetch('/api/calc', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        insurance_type: ins, age, coverage_type: cov,
        family_member_count: cov.includes('Myself only') ? 1 : fam,
        medical_conditions: med
      })
    });
    const d = await res.json();
    if (d.status !== 'success') throw new Error(d.message);

    const riskColor = d.risk_score <= 30 ? '#10b981' : d.risk_score <= 60 ? '#f59e0b' : '#ef4444';
    const claimColor= d.claim_probability <= 25 ? '#10b981' : d.claim_probability <= 55 ? '#f59e0b' : '#ef4444';

    resultEl.innerHTML = `
      <div class="calc-res-premium">${escHtml(d.premium_range)}</div>
      <div class="calc-res-label">Estimated Monthly Premium</div>

      <div class="calc-metrics">
        <div class="calc-metric">
          <div class="calc-metric-val" style="color:${riskColor}">${d.risk_score}<span style="font-size:14px">/100</span></div>
          <div class="calc-metric-lbl">Risk Score</div>
        </div>
        <div class="calc-metric-div"></div>
        <div class="calc-metric">
          <div class="calc-metric-val" style="color:${claimColor}">${d.claim_probability}<span style="font-size:14px">%</span></div>
          <div class="calc-metric-lbl">Claim Probability</div>
        </div>
      </div>

      <div class="calc-breakdown">
        <div class="calc-bk-title">Factors</div>
        <div class="calc-bk-row"><span>Age Risk</span><span class="calc-bk-val">${escHtml(d.breakdown.age_factor)}</span></div>
        <div class="calc-bk-row"><span>Health Risk</span><span class="calc-bk-val">${escHtml(d.breakdown.condition_risk)}</span></div>
        <div class="calc-bk-row"><span>Risk Category</span><span class="calc-bk-val" style="color:${riskColor}">${escHtml(d.risk_category)}</span></div>
      </div>`;
  } catch(e) {
    resultEl.innerHTML = '<div class="calc-result-idle" style="color:var(--danger)">⚠️ Could not calculate. Try again.</div>';
  }
}

// ════════════════════════════════════════════════════════════════════
// CLAIM PROBABILITY GAUGE — shown in sidebar after risk scored
// ════════════════════════════════════════════════════════════════════
function updateClaimGauge(pct) {
  const card = document.getElementById('claim-prob-card');
  const arc  = document.getElementById('claim-gauge-arc');
  const val  = document.getElementById('claim-pct-value');
  const desc = document.getElementById('claim-prob-desc');
  if (!card || !arc) return;

  card.classList.remove('hidden');
  const FULL_ARC = 157; // circumference of the half-circle (π×r = π×50)
  const dash = (pct / 100) * FULL_ARC;
  arc.setAttribute('stroke-dasharray', `${dash} ${FULL_ARC}`);

  const color = pct <= 25 ? '#10b981' : pct <= 55 ? '#f59e0b' : '#ef4444';
  arc.setAttribute('stroke', color);
  if (val) { val.textContent = pct + '%'; val.style.color = color; }

  let msg = '';
  if (pct <= 25) msg = '🟢 Low probability — healthy profile';
  else if (pct <= 45) msg = '🟡 Moderate — some risk factors present';
  else if (pct <= 65) msg = '🟠 Elevated — conditions affect likelihood';
  else msg = '🔴 High — recommend comprehensive coverage';
  if (desc) desc.textContent = msg;
}

// ════════════════════════════════════════════════════════════════════
// HOSPITAL NETWORK LOOKUP — shown in sidebar after health plan shown
// ════════════════════════════════════════════════════════════════════
async function loadHospitalNetwork(city, insuranceType) {
  if (!city) return;
  // Only for health insurance
  if (insuranceType && !insuranceType.toLowerCase().includes('health')) return;

  const card = document.getElementById('hospital-card');
  const list = document.getElementById('hospital-list');
  const cnt  = document.getElementById('hospital-count');
  if (!card || !list) return;

  card.classList.remove('hidden');
  list.innerHTML = '<div class="hosp-loading"><i class="fas fa-spinner fa-spin"></i> Loading hospitals…</div>';

  try {
    const res = await fetch('/api/hospitals', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ city, insurance_type: insuranceType || 'health' })
    });
    const d = await res.json();

    if (!d.hospitals || !d.hospitals.length) {
      list.innerHTML = `<div class="hosp-empty"><i class="fas fa-info-circle"></i> ${escHtml(d.message || 'No hospitals found')}</div>`;
      if (cnt) cnt.textContent = '';
      return;
    }

    list.innerHTML = d.hospitals.slice(0, 6).map(h => `
      <div class="hosp-item">
        <div class="hosp-name">${escHtml(h.name)}</div>
        <div class="hosp-meta">
          <span class="hosp-type">${escHtml(h.type)}</span>
          <span class="hosp-area"><i class="fas fa-location-dot"></i> ${escHtml(h.area)}</span>
        </div>
      </div>`).join('');

    if (cnt) cnt.textContent = `${d.total} cashless hospitals in ${d.city_matched}`;
    if (d.total > 6) {
      list.innerHTML += `<div class="hosp-more">+${d.total - 6} more hospitals in your insurer's network</div>`;
    }
  } catch(e) {
    list.innerHTML = '<div class="hosp-empty">Could not load hospital data</div>';
  }
}

// ═══════════════════════════════════════════════════════════════════
// AI POLICY DOCUMENT READER
// ═══════════════════════════════════════════════════════════════════
function openPolicyReader() {
  document.getElementById('pr-overlay')?.classList.remove('hidden');
}
function closePolicyReader() {
  document.getElementById('pr-overlay')?.classList.add('hidden');
}
function resetPolicyReader() {
  document.getElementById('pr-results')?.classList.add('hidden');
  document.getElementById('pr-upload-zone')?.classList.remove('hidden');
  document.getElementById('pr-progress-wrap')?.classList.add('hidden');
  document.getElementById('pr-progress-bar').style.width = '0%';
}

function handlePRDrop(e) {
  const file = e.dataTransfer?.files?.[0];
  if (file) handlePRFile(file);
}

async function handlePRFile(file) {
  if (!file) return;
  const ext = file.name.split('.').pop().toLowerCase();
  const ALLOWED = ['pdf','jpg','jpeg','png','docx','txt'];
  if (!ALLOWED.includes(ext)) {
    showToast('❌ Unsupported format. Use PDF, JPG, PNG, DOCX, or TXT.', 'error');
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    showToast('❌ File too large. Max 15MB.', 'error');
    return;
  }

  // Show progress
  const zone    = document.getElementById('pr-upload-zone');
  const progWrap= document.getElementById('pr-progress-wrap');
  const progBar = document.getElementById('pr-progress-bar');
  const progLbl = document.getElementById('pr-progress-label');

  zone?.classList.add('hidden');
  progWrap?.classList.remove('hidden');

  // Animate progress
  const steps = [
    [15, 'Uploading document…'],
    [35, 'Extracting text with OCR…'],
    [60, 'Analyzing coverage & terms…'],
    [80, 'Finding gaps & exclusions…'],
    [92, 'Generating recommendations…'],
  ];
  let stepIdx = 0;
  const stepInterval = setInterval(() => {
    if (stepIdx < steps.length) {
      const [pct, lbl] = steps[stepIdx++];
      if (progBar) progBar.style.width = pct + '%';
      if (progLbl) progLbl.textContent = lbl;
    } else {
      clearInterval(stepInterval);
    }
  }, 900);

  try {
    const form = new FormData();
    form.append('file', file);
    form.append('user_id', userId);

    const res  = await fetch('/api/policy-reader', { method: 'POST', body: form });
    const data = await res.json();
    clearInterval(stepInterval);

    if (progBar) progBar.style.width = '100%';
    if (progLbl) progLbl.textContent = 'Analysis complete ✅';
    await sleep(600);

    renderPolicyReaderResults(data);
  } catch(e) {
    clearInterval(stepInterval);
    progWrap?.classList.add('hidden');
    zone?.classList.remove('hidden');
    showToast('❌ Analysis failed: ' + (e.message || 'Please try again'), 'error');
  }
}

function renderPolicyReaderResults(d) {
  const results = document.getElementById('pr-results');
  const progWrap= document.getElementById('pr-progress-wrap');
  if (!results) return;

  progWrap?.classList.add('hidden');

  // Populate badge
  const badge = document.getElementById('pr-results-badge');
  if (badge) badge.textContent = d.policy_type || 'Insurance Policy';

  // Summary
  const sumEl = document.getElementById('pr-summary');
  if (sumEl) sumEl.textContent = d.summary || 'Policy analyzed successfully.';

  // Coverage
  const covList = document.getElementById('pr-coverage-list');
  if (covList) covList.innerHTML = (d.coverage_found || [])
    .map(c => `<li>${escHtml(c)}</li>`).join('') || '<li>No explicit coverage found</li>';

  // Gaps
  const gapList = document.getElementById('pr-gaps-list');
  if (gapList) gapList.innerHTML = (d.gaps || [])
    .map(g => `<li>${escHtml(g)}</li>`).join('') || '<li>No gaps detected</li>';

  // Exclusions
  const excList = document.getElementById('pr-exclusions-list');
  if (excList) excList.innerHTML = (d.exclusions || [])
    .map(x => `<li>${escHtml(x)}</li>`).join('') || '<li>No exclusions listed</li>';

  // Recommendations
  const recList = document.getElementById('pr-recs-list');
  if (recList) recList.innerHTML = (d.recommendations || [])
    .map(r => `<li>${escHtml(r)}</li>`).join('') || '<li>No specific recommendations</li>';

  results.classList.remove('hidden');
}

// ═══════════════════════════════════════════════════════════════════
// GAMIFICATION — XP points, level bar, badge popups
// ═══════════════════════════════════════════════════════════════════
async function awardXP(stage) {
  if (!userId) return;
  try {
    const res = await fetch('/api/xp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, stage, profile: {} })
    });
    const d = await res.json();
    if (d.status !== 'success') return;

    // Update XP bar
    updateXPBar(d.xp_total, d.level_name, d.level_icon, d.next_threshold);

    // Show badge popups for new badges
    if (d.new_badges && d.new_badges.length) {
      for (let i = 0; i < d.new_badges.length; i++) {
        setTimeout(() => showBadgePopup(d.new_badges[i]), i * 1600);
      }
    }

    // Level-up celebration
    if (d.levelled_up) {
      setTimeout(() => showLevelUp(d.level_name, d.level_icon), d.new_badges?.length ? 2400 : 200);
    }
  } catch(e) { /* silent */ }
}

function updateXPBar(total, levelName, levelIcon, nextThresh) {
  const wrap   = document.getElementById('xp-bar-wrap');
  const fill   = document.getElementById('xp-bar-fill');
  const name   = document.getElementById('xp-level-name');
  const icon   = document.getElementById('xp-level-icon');
  const totEl  = document.getElementById('xp-total');

  if (!wrap) return;
  wrap.classList.remove('hidden');
  if (icon)  icon.textContent  = levelIcon  || '🌱';
  if (name)  name.textContent  = levelName  || 'Beginner';
  if (totEl) totEl.textContent = total + ' XP';

  // Progress to next level
  const pct = nextThresh > 0 ? Math.min(100, Math.round((total / nextThresh) * 100)) : 100;
  if (fill) {
    fill.style.transition = 'width 1s cubic-bezier(.22,.61,.36,1)';
    fill.style.width = pct + '%';
  }
}

function showBadgePopup(badge) {
  if (!badge) return;
  const popup = document.getElementById('xp-popup');
  const icon  = document.getElementById('xp-popup-icon');
  const name  = document.getElementById('xp-popup-badge');
  const pts   = document.getElementById('xp-popup-pts');
  if (!popup) return;
  if (icon) icon.textContent = badge.icon  || '🏅';
  if (name) name.textContent = badge.name  || 'Achievement';
  if (pts)  pts.textContent  = '+' + (badge.xp || 10) + ' XP';
  popup.classList.remove('hidden');
  popup.classList.add('xp-popup-in');
  setTimeout(() => {
    popup.classList.remove('xp-popup-in');
    popup.classList.add('xp-popup-out');
    setTimeout(() => {
      popup.classList.add('hidden');
      popup.classList.remove('xp-popup-out');
    }, 500);
  }, 2800);
}

function showLevelUp(levelName, levelIcon) {
  showToast(`⬆️ Level Up! You're now ${levelIcon} ${levelName}!`, 'success');
  spawnConfetti();
}

// Hook XP into doSend — award XP whenever stage advances
const _origDoSend_xp = doSend;
// Patch: call awardXP after successful stage change
document.addEventListener('policybot:stage', e => {
  if (e.detail?.stage) awardXP(e.detail.stage);
});
