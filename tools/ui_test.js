const fs = require('fs');
const { JSDOM } = require('jsdom');
let html = fs.readFileSync(process.env.DASH || '../templates/dashboard.html', 'utf8').replace('{{ bot_name }}', 'ApexTick');
const fails = [];
const check = (name, cond, extra = '') => { console.log((cond ? 'PASS ' : 'FAIL ') + name + (extra ? '  [' + extra + ']' : '')); if (!cond) fails.push(name); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- fake server -----------------------------------------------------------------------------
const XSS = '<img src=x onerror="window.__pwned=1">';
let state;
function resetState() {
  state = {
    status: { status: 'RUNNING', api_ok: true, btc_gate_allowed: false, btc_gate_reason: 'Sell pressure (73%) exceeds maximum', ws_connected: true,
              execution_mode: 'PAPER TRADING', macro_regime: 'TRENDING', btc_gate_override: true, macro_halt: false, prices_age_seconds: 2.0,
              btc_ema_trend: 'BEARISH', btc_market_state: 'TREND', futures_bot_state: 'NORMAL', futures_bot_reason: 'Normal operation' },
    sent: { sentiment: 'NEUTRAL', trend: 'BULLISH', ema_trend: 'BEARISH', market_state: 'TREND', atr: '611.32', change_24h: '+1.00%', buy_pressure: '26.0%', sell_pressure: '74.0%' },
    spot: { total_profit: 7.26, win_rate: '95.00%', open_positions: 1, portfolio_value: 120.5, cash_balance: 100.0, unrealized_pnl: -3.75 },
    fut: { completed_trades: 5, win_rate: '40.00%', net_pnl: -7.66, portfolio_value: 99.0, cash_balance: 90.0, unrealized_pnl: 5.2, open_positions: 3 },
    pos: [
      { id: 1, type: 'futures', pair: 'MMT/USDT', direction: 'LONG', entry: '$0.26650', live_price: '$0.31090', size: '$2.69 (15x)', pnl_usdt: 6.73, roi_pct: 249.9,
        pnl_formatted: '$6.73 (+249.90%)', tp1: false, current_sl: 0.2401, dynamic_tp1: 0.3457, watermark: 0.3109, leverage: 15, banked_usdt: 0, unrealized_usdt: 6.73 },
      { id: 2, type: 'futures', pair: 'XAUT/USDT', direction: 'SHORT', entry: '$3747.67000', live_price: '$3729.28000', size: '$0.70 left of $2.80 (15x)', pnl_usdt: 0.38,
        roi_pct: 13.6, pnl_formatted: '$0.38 (+13.57%)', tp1: true, current_sl: 3747.67, dynamic_tp1: 3707.56, watermark: 3692.69, leverage: 15, banked_usdt: 0.337, unrealized_usdt: 0.05 },
      { id: 3, type: 'spot', pair: XSS, direction: 'SPOT', entry: '$0.00000', live_price: '$0.00000', entry_raw: 0.00000353, live_raw: 0.00000351, size: '$17.10', pnl_usdt: -0.9, roi_pct: -5,
        pnl_formatted: '$-0.90 (-5.00%)', tp1: false, current_sl: 0, dynamic_tp1: 10.12, watermark: 10 },
    ],
    powder: [{ pair: 'AAA/USDT', last_price: 1.5, oi_spot_ratio: 6.2 }],
    smc: [{ pair: 'ZZZ/USDT', pool_type: 'FVG', direction: 'BULLISH', top_price: 2, bottom_price: 1 }],
    hist: { futures_trades: [{ direction: 'SHORT', pair: XSS, close_type: '<b>STRUCTURAL SL</b>', realized_pnl: -0.34, pnl_pct: -11.5, closed_at: '2026-11-19T04:46:55.123+00:00' }],
            spot_trades: [{ pair: 'LUNC/USDT', exit_price: 0.000049, realized_pnl: 3.69, profit_pct: 21.8, closed_at: '2026-11-19T14:49:00' }] },
    perf: { mode: 'PAPER',
      futures: { trades: 48, wins: 8, losses: 40, win_rate: 16.67, net_pnl: -15.25, gross_profit: 10.5, gross_loss: -25.75, profit_factor: 0.408, avg_win: 1.31, avg_loss: -0.64,
        payoff_ratio: 2.05, breakeven_win_rate: 32.8, expectancy: -0.32, best_trade: 1.82, worst_trade: -1.35, current_streak: { type: 'L', length: 18 },
        longest_win_streak: { length: 5, start: '2026-11-21T04:12:44+00:00', end: '2026-11-21T04:47:52+00:00', net: 8.31 },
        longest_loss_streak: { length: 20, start: '2026-11-21T09:23:41+00:00', end: '2026-11-21T16:06:03+00:00', net: -12.95 },
        avg_hold_minutes: 95, pnl_24h: -11.5, pnl_7d: -15.25, tp1_hits: 9, stop_outs: 40, first_close: '2026-11-21T04:12:44+00:00', last_close: '2026-11-22T09:50:42+00:00',
        by_direction: { LONG: { trades: 20, win_rate: 20, net_pnl: -5.1 }, SHORT: { trades: 28, win_rate: 14.3, net_pnl: -10.15 } } },
      spot: { trades: 5, wins: 5, losses: 0, win_rate: 100, net_pnl: 2.14, gross_profit: 2.14, gross_loss: 0, profit_factor: null, avg_win: 0.43, avg_loss: 0, payoff_ratio: null,
        breakeven_win_rate: null, expectancy: 0.43, best_trade: 0.9, worst_trade: 0.1, current_streak: { type: 'W', length: 5 }, longest_win_streak: { length: 5, start: '2026-11-21T10:00:00+00:00', end: '2026-11-22T08:00:00+00:00', net: 2.14 },
        longest_loss_streak: null, avg_hold_minutes: 600, pnl_24h: 1.0, pnl_7d: 2.14, first_close: '2026-11-21T10:00:00+00:00', last_close: '2026-11-22T08:00:00+00:00' } },
    logs: [{ timestamp: '2026-11-19T05:00:00.1+00:00', level: 'ERROR', source: 'x', message: 'bad <script>window.__pwned=2</script> news' }],
  };
}
const routes = { '/api/performance': 'perf', '/api/status': 'status', '/api/btc-sentiment': 'sent', '/api/statistics': 'spot', '/api/futures/statistics': 'fut', '/api/positions': 'pos',
                 '/api/market/powder-keg': 'powder', '/api/market/smc-zones': 'smc', '/api/trades': 'hist', '/api/logs': 'logs' };
const calls = [];
let adminReply = null;
function fakeFetch(url, opts = {}) {
  calls.push({ url, opts });
  const mk = (code, body) => Promise.resolve({ ok: code >= 200 && code < 300, status: code, json: () => Promise.resolve(body) });
  if (opts.method === 'POST') return adminReply ? mk(adminReply.code, adminReply.body) : mk(200, { status: 'success' });
  const key = routes[url.split('?')[0]];
  if (!key) return mk(404, {});
  if (state.fail && state.fail.includes(key)) return mk(500, {});
  return mk(200, state[key]);
}

async function boot(mutate, hidden) {
  resetState(); if (mutate) mutate(state); calls.length = 0; adminReply = null;
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
    beforeParse(w) { w.fetch = fakeFetch; if (hidden) Object.defineProperty(w.document, 'hidden', { value: true, configurable: true }); } });
  await sleep(120);
  return dom;
}
const text = (dom, id) => dom.window.document.getElementById(id).textContent;

(async () => {
  // ================= rendering
  let dom = await boot(); let d = dom.window.document;
  check('page boots and renders positions', d.querySelectorAll('#positions-tbody tr').length === 3);
  check('XSS: pair name with <img onerror> is shown as text, not executed', !d.querySelector('#positions-tbody img') && !dom.window.__pwned && d.getElementById('positions-tbody').textContent.includes('<img src=x'));
  check('XSS: close type / log text is escaped', !d.querySelector('#futures-history-tbody b') && !d.querySelector('#logs-box script') && !dom.window.__pwned && d.getElementById('logs-box').textContent.includes('<script>'));
  check('KPIs: futures net PnL, win rate, open P/L', text(dom, 'kpi-fut-pnl') === '-$7.66' && text(dom, 'kpi-fut-wr') === '40.00%' && text(dom, 'kpi-fut-unreal') === '+$5.20', text(dom, 'kpi-fut-unreal'));
  check('KPI: portfolio = equity of both books, cash shown', text(dom, 'kpi-portfolio') === '$219.50' && text(dom, 'kpi-cash') === '$190.00', text(dom, 'kpi-portfolio') + ' / ' + text(dom, 'kpi-cash'));
  check('KPI: open positions split', text(dom, 'kpi-open-pos') === '4' && text(dom, 'kpi-open-sub') === 'Futures 3 \u00b7 Spot 1' );
  const rows = [...d.querySelectorAll('#positions-tbody tr')];
  const mmt = rows[0].textContent;
  check('MMT row: stop beyond liquidation is flagged', mmt.includes('STOP BEYOND LIQUIDATION') && rows[0].querySelector('.risk-line.bad'), mmt.match(/STOP[^)]*\)/)?.[0]);
  check('MMT row shows SL and TP1 distances', mmt.includes('SL: $0.24010 (-9.91%)') && mmt.includes('TP1: $0.34570 (+29.72%)'), mmt.replace(/\s+/g, ' ').slice(0, 160));
  const xaut = rows[1].textContent;
  check('TP1-secured row: breakeven stop, banked + runner shown, no risk line', xaut.includes('(breakeven)') && xaut.includes('banked +$0.34 + runner +$0.05') && !rows[1].querySelector('.risk-line'), xaut.replace(/\s+/g, ' ').slice(0, 200));
  check('spot row: no stop shown as "none", not $0.00000000', rows[2].textContent.includes('SL: none'));
  check('micro-priced coin keeps its price (PEPE-style 0.00000353, not $0.00000000)', rows[2].textContent.includes('$0.00000353') && !rows[2].textContent.includes('$0.00000000'), rows[2].textContent.replace(/\s+/g, ' ').slice(0, 120));
  check('Force Close buttons exist in PAPER mode', d.querySelectorAll('button[data-act="close"]').length === 3);
  check('BTC gate reason is visible', text(dom, 'btc-gate-reason').includes('Sell pressure') && text(dom, 'btc-gate') === 'BLOCK');
  check('BTC override switch follows the server (was always OFF after reload)', d.getElementById('btc-override-toggle').checked === true);
  check('override banner shown', [...d.querySelectorAll('#banners .banner')].some((b) => b.textContent.includes('BTC GATE OVERRIDDEN')));
  check('WS badge reflects the real stream state', text(dom, 'ws-status') === 'WS CONNECTED');
  check('history: dates cleaned, wins/losses signed', d.getElementById('spot-history-tbody').textContent.includes('+$3.69') && d.getElementById('futures-history-tbody').textContent.includes('-$0.34') && d.getElementById('spot-history-tbody').textContent.includes('2026-11-19 14:49:00'));
  const futRow = d.querySelector('#futures-history-tbody tr');
  check('futures exits show a Type column with the SHORT tag', futRow.querySelector('.tag.short') && futRow.querySelector('.tag.short').textContent === 'SHORT' && [...d.querySelectorAll('.table-container table')].some((t) => [...t.querySelectorAll('th')].map((x) => x.textContent).join('|') === 'Date/Time|Pair|Type|Exit Type|Realized PnL|PnL %'));
  check('futures exits row has 6 cells, spot exits still 5', futRow.children.length === 6 && d.querySelector('#spot-history-tbody tr').children.length === 5);
  const pf = d.getElementById('perf-fut').textContent.replace(/\s+/g, ' ');
  check('performance panel: win rate vs break-even, profit factor, expectancy', pf.includes('16.7%') && pf.includes('break-even needs 32.8%') && pf.includes('0.41') && pf.includes('-$0.32'), pf.slice(0, 160));
  check('performance panel: longest LOSING streak with its window', pf.includes('20 in a row') && pf.includes('11-21 09:23 \u2192 11-21 16:06 UTC \u00b7 -$12.95'), pf.match(/20 in a row[^A-Z]*/)?.[0]);
  check('performance panel: longest WIN streak, current streak, long vs short', pf.includes('5 in a row') && pf.includes('11-21 04:12 \u2192 11-21 04:47 UTC \u00b7 +$8.31') && pf.includes('L18') && pf.includes('LONG 20') && pf.includes('SHORT 28'));
  check('performance panel: win-rate tile is red below break-even', d.querySelector('#perf-fut .perf-tile .text-loss') !== null);
  const ps = d.getElementById('perf-spot').textContent.replace(/\s+/g, ' ');
  check('spot performance: 100% wins, infinite profit factor, W5, no losing streak', ps.includes('100.0%') && ps.includes('\u221e') && ps.includes('W5') && ps.includes('none'), ps.slice(0, 140));
  check('range label shows the first and last close', d.getElementById('perf-fut-range').textContent === '11-21 04:12 \u2192 11-22 09:50 UTC');
  check('SMC / powder widgets render', d.getElementById('smc-tbody').textContent.includes('ZZZ/USDT') && d.getElementById('powder-tbody').textContent.includes('6.20x'));
  check('macro widget shows 1h EMA trend and market state separately from 24h trend', text(dom, 'btc-trend') === 'BULLISH' && text(dom, 'btc-ema-trend') === 'BEARISH' && text(dom, 'btc-market-state') === 'TREND');
  check('bot state badge shows BOT: NORMAL', text(dom, 'bot-state-status') === 'BOT: NORMAL' && d.getElementById('bot-state-status').className.includes('online'));
  dom.window.close();

  // ================= status variants
  dom = await boot((s) => { s.status.futures_bot_state = 'PROBATION'; s.status.futures_bot_reason = 'Loss streak L3 >= 3'; s.status.btc_market_state = 'CHOP'; });
  d = dom.window.document;
  check('PROBATION badge and danger banner render clearly', text(dom, 'bot-state-status') === 'BOT: PROBATION' && d.getElementById('bot-state-status').className.includes('offline') && [...d.querySelectorAll('#banners .banner.danger')].some((b) => b.textContent.includes('FUTURES PROBATION')));
  dom.window.close();

  dom = await boot((s) => { s.status.futures_bot_state = 'CAUTION'; s.status.futures_bot_reason = 'BTC CHOP (ER=0.14)'; s.status.btc_market_state = 'CHOP'; });
  d = dom.window.document;
  check('CAUTION badge and warn banner render clearly', text(dom, 'bot-state-status') === 'BOT: CAUTION' && d.getElementById('bot-state-status').className.includes('stale') && [...d.querySelectorAll('#banners .banner.warn')].some((b) => b.textContent.includes('FUTURES CAUTION')));
  dom.window.close();
  dom = await boot((s) => { s.status.ws_connected = false; s.status.macro_halt = true; s.status.status = 'HALTED'; s.status.prices_age_seconds = 45; });
  d = dom.window.document;
  check('WS down shows REST fallback + red badge', text(dom, 'ws-status').includes('REST FALLBACK') && d.getElementById('ws-status').className.includes('offline'));
  const bt = [...d.querySelectorAll('#banners .banner')].map((b) => b.textContent).join(' | ');
  check('macro-halt and stale-price banners shown', bt.includes('MACRO HALT') && bt.includes('PRICE FEED STALE (45s)'), bt.slice(0, 80));
  check('data badge warns about stale prices', text(dom, 'data-status').startsWith('PRICES STALE'), text(dom, 'data-status'));
  dom.window.close();

  dom = await boot((s) => { s.status.execution_mode = 'LIVE TRADING'; });
  d = dom.window.document;
  check('LIVE banner shown and the toggle is on', [...d.querySelectorAll('#banners .banner.danger')].some((b) => b.textContent.includes('LIVE FUNDS ACTIVE')) && d.getElementById('live-toggle').checked);
  check('LIVE mode: Force Close replaced by a disabled "Close on Binance"', d.querySelectorAll('button[data-act="close"]').length === 0 && [...d.querySelectorAll('.btn-close')].every((b) => b.disabled));
  dom.window.close();

  dom = await boot((s) => { s.fail = ['hist', 'logs', 'smc']; });
  d = dom.window.document;
  check('one failing endpoint does not blank the rest', d.querySelectorAll('#positions-tbody tr').length === 3 && text(dom, 'kpi-fut-pnl') === '-$7.66');
  dom.window.close();

  dom = await boot(null, true);
  check('hidden tab shows PAUSED, not a false "DASHBOARD OFFLINE"', text(dom, 'data-status') === 'PAUSED - TAB HIDDEN', text(dom, 'data-status'));
  dom.window.close();

  dom = await boot((s) => { s.fail = ['status', 'sent', 'spot', 'fut', 'pos']; });
  check('real outage still shows DASHBOARD OFFLINE', text(dom, 'data-status') === 'DASHBOARD OFFLINE', text(dom, 'data-status'));
  dom.window.close();

  dom = await boot((s) => { s.pos = []; });
  check('empty positions message', dom.window.document.getElementById('positions-tbody').textContent.includes('No active positions'));
  dom.window.close();

  // ================= admin flows (modal, masked key, confirmations)
  dom = await boot(); d = dom.window.document; const w = dom.window;
  const modal = d.getElementById('modal'), keyEl = d.getElementById('modal-key'), phraseEl = d.getElementById('modal-phrase'), ok = d.getElementById('modal-ok');
  const type = (el, v) => { el.value = v; el.dispatchEvent(new w.Event('input', { bubbles: true })); };
  check('admin key field is a masked password input', keyEl.type === 'password');

  d.querySelector('button[data-act="close"]').click(); await sleep(20);
  check('Force Close opens a confirmation (no browser prompt)', !modal.hidden && d.getElementById('modal-title').textContent.includes('Force close MMT/USDT') && d.getElementById('modal-text').textContent.includes('$6.73'));
  check('confirm is disabled until a key is typed', ok.disabled === true);
  d.getElementById('modal-cancel').click(); await sleep(20);
  check('cancel closes it and sends nothing', modal.hidden && !calls.some((c) => c.opts.method === 'POST'));

  d.querySelector('button[data-act="close"]').click(); await sleep(20);
  adminReply = { code: 200, body: { status: 'success', message: 'MMT/USDT closed. Final PnL $+6.70' } };
  type(keyEl, 'secret-key-123'); ok.click(); await sleep(60);
  const post = calls.find((c) => c.opts.method === 'POST');
  check('Force Close posts to the right URL with the admin header', post && post.url === '/api/trades/close/futures/1' && post.opts.headers['X-Admin-Key'] === 'secret-key-123', post && post.url);
  check('success toast shows the server message', d.getElementById('toast').textContent.includes('Final PnL $+6.70') && d.getElementById('toast').className === 'ok');
  check('key field is wiped after use', keyEl.value === '');

  calls.length = 0; adminReply = { code: 409, body: { error: 'Trade is already CLOSED' } };
  d.querySelector('button[data-act="close"]').click(); await sleep(20);
  check('key is remembered in memory for a few minutes (prefilled, still masked)', keyEl.value === 'secret-key-123' && !ok.disabled);
  ok.click(); await sleep(60);
  check('server error is shown to the user', d.getElementById('toast').textContent.includes('already CLOSED') && d.getElementById('toast').className === 'err');

  // liquidate all needs the typed word
  adminReply = { code: 200, body: { status: 'success', message: 'Liquidated 0 spot and 2 futures PAPER positions (PnL $+5.30).' } };
  w.panicSellAll(); await sleep(20);
  check('Liquidate All asks for the word LIQUIDATE', !phraseEl.hidden && d.getElementById('modal-box').className.includes('danger'));
  type(phraseEl, 'nope'); check('wrong word keeps Confirm disabled', ok.disabled);
  type(phraseEl, 'LIQUIDATE'); check('right word enables it', !ok.disabled);
  calls.length = 0; ok.click(); await sleep(60);
  check('liquidate-all posted, summary toast shown', calls.some((c) => c.url === '/api/trades/liquidate-all' && c.opts.method === 'POST') && d.getElementById('toast').textContent.includes('Liquidated 0 spot'));

  // LIVE toggle: typed word + refusal reverts the switch
  const live = d.getElementById('live-toggle'); calls.length = 0;
  adminReply = { code: 403, body: { error: 'LIVE mode is locked on this server. Set ALLOW_LIVE_TRADING=1 in .env and restart to unlock it.' } };
  live.checked = true; const p = w.toggleMode(); await sleep(20);
  check('LIVE switch needs the typed word LIVE', !phraseEl.hidden && phraseEl.placeholder.includes('LIVE'));
  type(phraseEl, 'LIVE'); type(keyEl, 'secret-key-123'); ok.click(); await p; await sleep(40);
  const tm = calls.find((c) => c.url === '/api/toggle-mode');
  check('toggle-mode sends "0" for LIVE', tm && JSON.parse(tm.opts.body).paper_trading_enabled === '0');
  check('refused LIVE switch reverts the toggle and explains why', live.checked === false && d.getElementById('toast').textContent.includes('LIVE mode is locked'));

  // wrong key -> cached key dropped
  adminReply = { code: 403, body: { error: 'Unauthorized' } };
  d.querySelector('button[data-act="close"]').click(); await sleep(20); type(keyEl, 'wrong'); ok.click(); await sleep(60);
  d.querySelector('button[data-act="close"]').click(); await sleep(20);
  check('a rejected key is NOT remembered', keyEl.value === '');
  d.getElementById('modal-cancel').click();

  // BTC override switch
  calls.length = 0; adminReply = { code: 200, body: { status: 'success', override: '0' } };
  const ov = d.getElementById('btc-override-toggle'); ov.checked = false; const p2 = w.toggleBTCGate(); await sleep(20);
  check('restoring the BTC gate asks for confirmation', !modal.hidden && d.getElementById('modal-title').textContent.includes('Restore'));
  type(keyEl, 'k'); ok.click(); await p2; await sleep(40);
  check('btc gate toggle sends the override flag', JSON.parse(calls.find((c) => c.url === '/api/toggle-btc-gate').opts.body).override === '0');

  // polling does not fight an open modal
  adminReply = null; live.checked = false; ov.checked = false;
  const p3 = w.toggleBTCGate(); ov.checked = true; await sleep(20);
  await w.eval('fetchData(true)'); await sleep(80);
  check('background refresh does not flip a switch while a dialog is open', ov.checked === true);
  d.getElementById('modal-cancel').click(); await p3;

  dom.window.close();
  console.log('\nFAILED:', fails.length ? fails : 'none');
  process.exit(fails.length ? 1 : 0);
})();
