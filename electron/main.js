const { app, BrowserWindow, Tray, Menu, nativeImage, shell, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');

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

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 400,
    height: 620,
    resizable: false,
    title: 'Ridea',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    },
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    backgroundColor: '#0f172a'
  });

  mainWindow.loadFile('recorder.html');

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
  // Use a simple colored icon - in production replace with proper icon files
  const icon = nativeImage.createEmpty();
  tray = new Tray(icon);

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '💡 Nahrať nápad',
      click: () => { mainWindow.show(); mainWindow.focus(); }
    },
    { type: 'separator' },
    {
      label: '🌐 Otvoriť Admin konzolu',
      click: () => shell.openExternal(API_BASE)
    },
    { type: 'separator' },
    {
      label: 'Ukončiť',
      click: () => {
        app.isQuitting = true;
        app.quit();
      }
    }
  ]);

  tray.setToolTip('Ridea');
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

app.whenReady().then(() => {
  createWindow();

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
