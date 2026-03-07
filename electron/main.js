const { app, BrowserWindow, Tray, Menu, nativeImage, shell, ipcMain, session, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const zlib = require('zlib');

// Allow cross-origin cookies from file:// -> ridea.onrender.com
app.commandLine.appendSwitch('disable-features', 'SameSiteByDefaultCookies,CookiesWithoutSameSiteMustBeSecure');

// Simple JSON store (replaces electron-store to avoid ESM issues)
let _storeData = {};
let _storePath = null;
function _loadStore() {
  if (!_storePath) _storePath = path.join(app.getPath('userData'), 'ridea-config.json');
  try { _storeData = JSON.parse(fs.readFileSync(_storePath, 'utf-8')); } catch {}
}
function storeGet(key, def) { _loadStore(); return _storeData[key] !== undefined ? _storeData[key] : def; }
function storeSet(key, value) { _loadStore(); _storeData[key] = value; fs.writeFileSync(_storePath, JSON.stringify(_storeData)); }

const API_BASE = storeGet('apiBase', 'https://ridea.onrender.com');

let mainWindow = null;
let tray = null;

// --- Find the real icon.ico file ---
function getIconPath() {
  // Try multiple locations where icon.ico might be
  const candidates = [
    path.join(__dirname, 'assets', 'icon.ico'),
    path.join(__dirname, 'icon.ico'),
    path.join(process.resourcesPath || __dirname, 'icon.ico'),
    path.join(app.getAppPath(), 'assets', 'icon.ico'),
    path.join(app.getAppPath(), 'icon.ico')
  ];
  for (const p of candidates) {
    try {
      if (fs.existsSync(p)) {
        console.log('Found icon at:', p);
        return p;
      }
    } catch {}
  }
  console.log('No icon.ico found, using fallback');
  return null;
}

// --- PNG icon generator (fallback if no .ico found) ---
function _crc32(buf) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i];
    for (let j = 0; j < 8; j++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
  }
  return (c ^ 0xFFFFFFFF) >>> 0;
}
function _pngChunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
  const t = Buffer.from(type, 'ascii');
  const crc = Buffer.alloc(4); crc.writeUInt32BE(_crc32(Buffer.concat([t, data])));
  return Buffer.concat([len, t, data, crc]);
}
function makeFallbackIcon(size) {
  // Purple square with RI text (basic fallback)
  const R = 81, G = 45, B = 109;
  const raw = Buffer.alloc(size * (1 + size * 4));
  for (let y = 0; y < size; y++) {
    const base = y * (1 + size * 4);
    raw[base] = 0;
    for (let x = 0; x < size; x++) {
      raw[base + 1 + x * 4]     = R;
      raw[base + 1 + x * 4 + 1] = G;
      raw[base + 1 + x * 4 + 2] = B;
      raw[base + 1 + x * 4 + 3] = 255;
    }
  }
  const ihdrData = Buffer.alloc(13);
  ihdrData.writeUInt32BE(size, 0); ihdrData.writeUInt32BE(size, 4);
  ihdrData[8] = 8; ihdrData[9] = 6;
  const sig = Buffer.from([0x89,0x50,0x4E,0x47,0x0D,0x0A,0x1A,0x0A]);
  const png = Buffer.concat([
    sig,
    _pngChunk('IHDR', ihdrData),
    _pngChunk('IDAT', zlib.deflateSync(raw)),
    _pngChunk('IEND', Buffer.alloc(0))
  ]);
  return nativeImage.createFromBuffer(png);
}

// Get the app icon as nativeImage
function getAppIcon() {
  const iconPath = getIconPath();
  if (iconPath) {
    try {
      return nativeImage.createFromPath(iconPath);
    } catch(e) {
      console.log('Failed to load icon from path:', e.message);
    }
  }
  return makeFallbackIcon(256);
}

// Get smaller icon for tray
function getTrayIcon() {
  const iconPath = getIconPath();
  if (iconPath) {
    try {
      const img = nativeImage.createFromPath(iconPath);
      // Resize to 16x16 for tray
      return img.resize({ width: 16, height: 16 });
    } catch(e) {
      console.log('Failed to load tray icon:', e.message);
    }
  }
  return makeFallbackIcon(16);
}

function createWindow() {
  const appIcon = getAppIcon();

  mainWindow = new BrowserWindow({
    width: 380,
    height: 580,
    resizable: false,
    title: 'Ridea',
    icon: appIcon,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    },
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    backgroundColor: '#512D6D'
  });

  // Remove native menu bar (File, Edit, View...)
  Menu.setApplicationMenu(null);

  // Load electron recorder from server (always up-to-date) with cache bypass
  mainWindow.loadURL(API_BASE + '/electron-recorder?v=' + Date.now());

  mainWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

function createTray() {
  const icon = getTrayIcon();
  tray = new Tray(icon);

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'RI - Nahrat napad',
      click: () => { mainWindow.show(); mainWindow.focus(); }
    },
    { type: 'separator' },
    {
      label: 'Otvorit Admin konzolu',
      click: () => shell.openExternal(API_BASE)
    },
    { type: 'separator' },
    {
      label: 'Ukoncit',
      click: () => {
        app.isQuitting = true;
        app.quit();
      }
    }
  ]);

  tray.setToolTip('Ridea - zachytavac napadov');
  tray.setContextMenu(contextMenu);

  tray.on('click', () => {
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// IPC handlers
ipcMain.handle('get-api-base', () => API_BASE);
ipcMain.handle('get-store', (event, key) => storeGet(key));
ipcMain.handle('set-store', (event, key, value) => storeSet(key, value));

function checkForUpdates() {
  const { autoUpdater } = require('electron-updater');
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-downloaded', (info) => {
    dialog.showMessageBox(mainWindow, {
      type: 'info',
      title: 'Aktualizacia pripravena',
      message: 'Verzia ' + info.version + ' je stiahnutata. Restartovat teraz?',
      buttons: ['Restartovat', 'Neskor'],
      defaultId: 0
    }).then(({ response }) => {
      if (response === 0) autoUpdater.quitAndInstall();
    });
  });

  autoUpdater.on('error', () => {});

  setTimeout(() => autoUpdater.checkForUpdates(), 3000);
}

app.whenReady().then(() => {
  // Allow Set-Cookie from ridea.onrender.com to be stored and sent back
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    const hdrs = details.responseHeaders || {};
    // Strip SameSite=Lax/Strict and replace with SameSite=None; Secure
    if (hdrs['set-cookie']) {
      hdrs['set-cookie'] = hdrs['set-cookie'].map(c =>
        c.replace(/;\s*SameSite=(Lax|Strict)/gi, '; SameSite=None')
         .replace(/;\s*Secure/gi, '')
         + '; SameSite=None; Secure'
      );
    }
    callback({ responseHeaders: hdrs });
  });

  createWindow();
  checkForUpdates();

  // Only create tray on supported platforms
  try { createTray(); } catch(e) { console.log('Tray not available:', e.message); }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    else { mainWindow.show(); mainWindow.focus(); }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  app.isQuitting = true;
});
