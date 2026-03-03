const API_BASE = 'https://ridea.onrender.com';
// For local dev, change to: const API_BASE = 'http://localhost:5001';

let mediaRecorder = null;
let audioChunks = [];
let audioBlob = null;
let timerInterval = null;
let seconds = 0;
let currentUser = null;

// ─── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  // Check saved session
  const saved = await chrome.storage.local.get(['user']);
  if (saved.user) {
    // Verify session still valid
    try {
      const r = await fetch(`${API_BASE}/api/current-user`, { credentials: 'include' });
      if (r.ok) {
        currentUser = await r.json();
        showRecorder();
        return;
      }
    } catch(e) {}
  }
  showLogin();
}

function showLogin() {
  document.getElementById('login-screen').style.display = '';
  document.getElementById('recorder-screen').style.display = 'none';
}

function showRecorder() {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('recorder-screen').style.display = '';
  document.getElementById('user-label').textContent = `👤 ${currentUser.name}`;
  if (currentUser.department) {
    document.getElementById('department').value = currentUser.department;
  }
}

// ─── Login ────────────────────────────────────────────────────────────────────
async function doLogin() {
  const email = document.getElementById('l-email').value.trim();
  const pass = document.getElementById('l-pass').value;
  const err = document.getElementById('l-err');
  err.style.display = 'none';

  try {
    const r = await fetch(`${API_BASE}/api/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password: pass }),
      credentials: 'include'
    });
    if (r.ok) {
      const d = await r.json();
      currentUser = { name: d.name, role: d.role, email };
      await chrome.storage.local.set({ user: currentUser });
      showRecorder();
    } else {
      const d = await r.json();
      err.textContent = d.error || 'Nesprávne údaje';
      err.style.display = 'block';
    }
  } catch(e) {
    err.textContent = 'Chyba pripojenia k serveru';
    err.style.display = 'block';
  }
}

async function doLogout() {
  try { await fetch(`${API_BASE}/api/logout`, { method: 'POST', credentials: 'include' }); } catch(e) {}
  await chrome.storage.local.remove(['user']);
  currentUser = null;
  showLogin();
}

function openAdmin() {
  chrome.tabs.create({ url: API_BASE });
}

// ─── Recording ────────────────────────────────────────────────────────────────
async function toggleRecord() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    stopRecording();
  } else {
    await startRecording();
  }
}

async function startRecording() {
  const dept = document.getElementById('department').value;
  const role = document.getElementById('role').value;
  if (!dept || !role) {
    showStatus('Vyberte oddelenie a rolu', 'error');
    return;
  }

  try {
    // Request microphone via offscreen or directly
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];

    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm';

    mediaRecorder = new MediaRecorder(stream, { mimeType });
    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRecorder.onstop = () => {
      audioBlob = new Blob(audioChunks, { type: mimeType });
      stream.getTracks().forEach(t => t.stop());
      document.getElementById('send-btn').classList.add('show');
      document.getElementById('discard-lnk').classList.add('show');
    };
    mediaRecorder.start(1000);

    const btn = document.getElementById('rec-btn');
    btn.className = 'record-btn rec';
    document.getElementById('btn-ic').textContent = '⬛';
    document.getElementById('btn-tx').textContent = 'Zastaviť';

    seconds = 0;
    document.getElementById('timer').classList.add('show');
    timerInterval = setInterval(() => {
      seconds++;
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      document.getElementById('timer-tx').textContent = `${m}:${s.toString().padStart(2,'0')}`;
    }, 1000);

    clearStatus();
  } catch(e) {
    showStatus(e.name === 'NotAllowedError' ? 'Prístup k mikrofónu odmietnutý' : `Chyba: ${e.message}`, 'error');
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
  clearInterval(timerInterval);
  document.getElementById('timer').classList.remove('show');
  const btn = document.getElementById('rec-btn');
  btn.className = 'record-btn idle';
  document.getElementById('btn-ic').textContent = '🔴';
  document.getElementById('btn-tx').textContent = 'Nahrať znova';
}

function discardRecording() {
  audioBlob = null;
  audioChunks = [];
  document.getElementById('send-btn').classList.remove('show');
  document.getElementById('discard-lnk').classList.remove('show');
  document.getElementById('rec-btn').className = 'record-btn idle';
  document.getElementById('btn-ic').textContent = '🔴';
  document.getElementById('btn-tx').textContent = 'Nahrať nápad';
  clearStatus();
}

async function submitIdea() {
  if (!audioBlob) return;
  const dept = document.getElementById('department').value;
  const role = document.getElementById('role').value;

  const sendBtn = document.getElementById('send-btn');
  sendBtn.disabled = true;
  sendBtn.textContent = '⏳ Odosiela sa...';

  showStatus('Nahrávam... môže trvať 10–30 sekúnd', 'info');
  document.getElementById('progress').classList.add('show');
  animateProgress();

  try {
    const formData = new FormData();
    formData.append('audio', audioBlob, `idea_${Date.now()}.webm`);
    formData.append('department', dept);
    formData.append('role', role);

    const r = await fetch(`${API_BASE}/api/ideas/upload`, {
      method: 'POST',
      body: formData,
      credentials: 'include'
    });

    document.getElementById('progress-fill').style.width = '100%';

    if (r.ok) {
      const d = await r.json();
      showStatus(`✅ Nápad odoslaný!`, 'success');
      setTimeout(discardRecording, 3000);
    } else {
      const d = await r.json().catch(() => ({}));
      showStatus(d.error || 'Chyba pri odosielaní', 'error');
    }
  } catch(e) {
    showStatus(`Chyba: ${e.message}`, 'error');
  } finally {
    sendBtn.disabled = false;
    sendBtn.textContent = '✅ Odoslať nápad';
    setTimeout(() => {
      document.getElementById('progress').classList.remove('show');
      document.getElementById('progress-fill').style.width = '0%';
    }, 2000);
  }
}

let progInterval = null;
function animateProgress() {
  let w = 0;
  clearInterval(progInterval);
  progInterval = setInterval(() => {
    w = Math.min(w + 3, 85);
    document.getElementById('progress-fill').style.width = w + '%';
    if (w >= 85) clearInterval(progInterval);
  }, 500);
}

function showStatus(msg, type) {
  const el = document.getElementById('status-msg');
  el.textContent = msg;
  el.className = `status ${type}`;
}
function clearStatus() {
  document.getElementById('status-msg').className = 'status';
}

// Enter key for login
document.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    const ls = document.getElementById('login-screen');
    if (ls.style.display !== 'none') doLogin();
  }
});

init();
