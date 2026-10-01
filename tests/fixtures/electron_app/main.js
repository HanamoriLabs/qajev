// QAJev's own Electron fixture for the Native Electron tests: one window with a tiny keyboard-and-mouse game.
const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const profile = process.argv.find((a) => a.startsWith('--profile='));
if (profile) app.setPath('userData', profile.slice('--profile='.length));
app.whenReady().then(() => {
  const w = new BrowserWindow({ width: 800, height: 600, show: true });
  w.loadFile(path.join(__dirname, 'index.html'));
});
app.on('window-all-closed', () => app.quit());
