// Adapter for tests/fixtures/electron_app: the screen from the game's mode, its state, and Escape as an action.
window.__qajevAdapter = {
  observe() {
    const g = window.game || {};
    const screen = { menu: 'MENU', playing: 'GAME', paused: 'PAUSED' }[g.mode] || 'BOOT';
    const actions = g.mode === 'playing' ? [{ id: 'pause', label: 'Pause the game', kind: 'key', key: 'Escape' }] : [];
    return { screen, actions, state: { mode: g.mode, ticks: g.ticks } };
  },
};
