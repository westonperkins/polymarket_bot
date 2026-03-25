"""Lightweight local dashboard — connects directly to Supabase, no VPS needed.

Run: python3 local_dashboard.py
View: http://localhost:3000
"""

import asyncio
import os
from functools import partial
from pathlib import Path

import psycopg2
import psycopg2.extras
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
PORT = 3000


def get_conn():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    conn.autocommit = True
    return conn


def query_state(conn):
    """Build dashboard state from Supabase."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Live portfolio
    cur.execute("""
        SELECT COALESCE(SUM(pnl), 0) AS total_pnl
        FROM trades WHERE outcome IN ('win', 'loss') AND trading_mode = 'live'
    """)
    live_pnl = float(cur.fetchone()["total_pnl"])

    cur.execute("SELECT value FROM settings WHERE key = 'live_starting_balance'")
    row = cur.fetchone()
    live_starting = float(row["value"]) if row else 9.11
    live_balance = live_starting + live_pnl

    # Trade stats
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE outcome IN ('win','loss')) AS total,
            COUNT(*) FILTER (WHERE outcome = 'win') AS wins,
            COUNT(*) FILTER (WHERE outcome = 'loss') AS losses,
            COUNT(*) FILTER (WHERE outcome = 'skip') AS skips
        FROM trades WHERE trading_mode = 'live'
    """)
    s = cur.fetchone()
    total = s["total"] or 0
    wins = s["wins"] or 0
    # Detailed skip breakdown
    cur.execute("""
        SELECT
            COALESCE(t.skip_reason, CASE WHEN s.final_vote = 'ABSTAIN' THEN 'no_consensus' ELSE 'order_rejected' END) AS reason,
            COUNT(*) AS cnt
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE t.trading_mode = 'live' AND t.outcome = 'skip'
        GROUP BY reason
        ORDER BY cnt DESC
    """)
    skip_detail = {}
    for row in cur.fetchall():
        skip_detail[row["reason"]] = row["cnt"]

    stats = {
        "total": total, "wins": wins,
        "losses": s["losses"] or 0, "skips": s["skips"] or 0,
        "skip_detail": skip_detail,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
    }

    # Best/worst trades
    cur.execute("""
        SELECT MAX(pnl) AS best, MIN(pnl) AS worst
        FROM trades WHERE outcome IN ('win', 'loss') AND trading_mode = 'live'
    """)
    bw = cur.fetchone()
    best_trade = float(bw["best"]) if bw["best"] is not None else 0.0
    worst_trade = float(bw["worst"]) if bw["worst"] is not None else 0.0

    # Peak balance
    cur.execute("""
        SELECT COALESCE(MAX(portfolio_balance_after), 0) AS peak
        FROM trades WHERE portfolio_balance_after IS NOT NULL AND trading_mode = 'live'
    """)
    peak = float(cur.fetchone()["peak"])

    # Recent trades
    cur.execute("""
        SELECT id, timestamp, market_id, side, entry_odds, position_size,
               payout_rate, confidence_level, outcome, pnl,
               portfolio_balance_after, trading_mode
        FROM trades WHERE trading_mode = 'live'
        ORDER BY id DESC LIMIT 100
    """)
    trades = [dict(r) for r in cur.fetchall()]

    # Attach signals to each trade
    for t in trades:
        cur.execute("SELECT * FROM signals WHERE trade_id = %s", (t["id"],))
        sig = cur.fetchone()
        t["signals"] = dict(sig) if sig else None

    # Equity curve — balance after each settled trade
    cur.execute("""
        SELECT id, timestamp, portfolio_balance_after, outcome, pnl
        FROM trades
        WHERE trading_mode = 'live'
          AND outcome IN ('win', 'loss')
          AND portfolio_balance_after IS NOT NULL
        ORDER BY id ASC
    """)
    equity_curve = [dict(r) for r in cur.fetchall()]

    cur.close()

    return {
        "portfolio": {
            "balance": round(live_balance, 2),
            "starting": live_starting,
            "pnl": round(live_pnl, 2),
            "pnl_pct": round(live_pnl / live_starting * 100, 2) if live_starting > 0 else 0,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "peak_balance": peak,
            **stats,
        },
        "trades": trades,
        "equity_curve": equity_curve,
    }


HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>Polymarket Dashboard</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3;
    --dim:#8b949e; --green:#3fb950; --red:#f85149; --cyan:#39d2c0; --yellow:#d29922; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'SF Mono','Consolas',monospace; background:var(--bg);
    color:var(--text); padding:20px; max-width:900px; margin:0 auto; }
  .header { text-align:center; padding:12px; margin-bottom:20px;
    border:1px solid var(--cyan); border-radius:8px; }
  .header h1 { font-size:16px; color:var(--cyan); letter-spacing:1px; }
  .header .sub { font-size:11px; color:var(--dim); margin-top:4px; }
  .card { border:1px solid var(--border); border-radius:8px; padding:16px;
    background:var(--card); margin-bottom:16px; }
  .card h2 { font-size:12px; text-transform:uppercase; letter-spacing:1px;
    margin-bottom:10px; padding-bottom:6px; border-bottom:1px solid var(--border);
    color:var(--green); }
  .balance { font-size:32px; font-weight:700; margin:4px 0 8px; }
  .pnl { font-size:15px; margin-bottom:12px; }
  .pos { color:var(--green); } .neg { color:var(--red); }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:2px 24px; }
  .row { display:flex; justify-content:space-between; padding:3px 0; font-size:12px; }
  .lbl { color:var(--dim); } .val { font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:11px; margin-top:8px; }
  th { text-align:left; padding:5px 6px; color:var(--dim); border-bottom:1px solid var(--border); }
  td { padding:5px 6px; border-bottom:1px solid var(--border); }
  .r { text-align:right; }
  .badge { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; font-weight:700; }
  .b-win { background:rgba(63,185,80,.15); color:var(--green); }
  .b-loss { background:rgba(248,81,73,.15); color:var(--red); }
  .b-skip { background:rgba(139,148,158,.15); color:var(--dim); }
  .b-pending { background:rgba(210,153,34,.15); color:var(--yellow); }
  .b-failed { background:rgba(248,81,73,.1); color:#f0883e; }
  .trade-row { cursor:pointer; transition:background 0.15s; }
  .trade-row:hover { background:rgba(88,166,255,0.06); }
  .trade-row td:first-child::before { content:'\\25B6'; font-size:8px; margin-right:4px;
    color:var(--dim); display:inline-block; transition:transform 0.2s; }
  .trade-row.expanded td:first-child::before { transform:rotate(90deg); }
  .trade-detail { display:none; }
  .trade-detail.open { display:table-row; }
  .detail-inner { padding:10px 12px; background:rgba(22,27,34,0.8);
    border-left:3px solid var(--cyan); }
  .detail-grid { display:grid; grid-template-columns:1fr 1fr; gap:3px 24px; font-size:12px; }
  .detail-section { font-size:11px; font-weight:700; text-transform:uppercase;
    letter-spacing:0.5px; color:var(--cyan); margin:8px 0 4px; grid-column:1/-1; }
  .detail-section:first-child { margin-top:0; }
  .detail-row { display:flex; justify-content:space-between; padding:1px 0; }
  .detail-label { color:var(--dim); } .detail-value { font-weight:600; }
  .vote-up { color:var(--green); font-weight:700; }
  .vote-down { color:var(--red); font-weight:700; }
  .vote-abstain { color:var(--dim); }
  .toggle-btn { background:var(--card); color:var(--dim); border:1px solid var(--border);
    border-radius:4px; padding:3px 10px; cursor:pointer; font-family:inherit;
    font-size:11px; letter-spacing:1px; font-weight:600; float:right; margin-top:-28px; }
  .toggle-btn:hover { border-color:var(--cyan); color:var(--text); }
  .toggle-btn.active { border-color:var(--cyan); color:var(--cyan); background:rgba(57,210,192,0.1); }
  .footer { text-align:center; font-size:11px; color:var(--dim); margin-top:12px; }
</style>
</head><body>
<div class="header">
  <h1>POLYMARKET LIVE TRADING BOT</h1>
  <div class="sub">Direct from Supabase &middot; Auto-refreshes every 5s</div>
</div>
<div class="card">
  <h2>Portfolio</h2>
  <div id="portfolio">Loading...</div>
</div>
<div class="card" style="display:flex; gap:24px; align-items:center;">
  <div style="flex:1;">
    <h2 style="color:var(--dim)">Skip Breakdown</h2>
    <canvas id="skipChart" height="160" width="160"></canvas>
  </div>
  <div style="flex:2;">
    <div id="skipLegend" style="font-size:12px;"></div>
  </div>
</div>
<div class="card">
  <h2 style="color:var(--yellow)">Equity Curve</h2>
  <canvas id="equityChart" height="120"></canvas>
</div>
<div class="card">
  <h2 style="color:var(--cyan)">Recent Trades</h2>
  <button class="toggle-btn active" id="btn-hide-skips" onclick="toggleSkips()">HIDE SKIPS</button>
  <table><thead><tr>
    <th>#</th><th>Market</th><th>Side</th><th class="r">Cost</th><th class="r">R:R</th>
    <th>Result</th><th class="r">P&L</th><th class="r">Balance</th>
  </tr></thead><tbody id="tbody"><tr><td colspan="8" style="color:var(--dim);text-align:center">Loading...</td></tr></tbody></table>
</div>
<div class="footer" id="footer">Connecting...</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
let equityChart = null;
let skipChart = null;
let hideSkips = localStorage.getItem('hideSkips') !== 'false';
function toggleSkips() {
  hideSkips = !hideSkips;
  localStorage.setItem('hideSkips', hideSkips);
  const btn = document.getElementById('btn-hide-skips');
  btn.textContent = hideSkips ? 'HIDE SKIPS' : 'SHOW ALL';
  btn.className = 'toggle-btn' + (hideSkips ? ' active' : '');
  poll();
}
// Init button state
if (!hideSkips) {
  document.getElementById('btn-hide-skips').textContent = 'SHOW ALL';
  document.getElementById('btn-hide-skips').className = 'toggle-btn';
}
const fmt = (n,d=2) => n!=null ? n.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}) : '-';
function voteHtml(v) {
  if (v==='Up') return '<span class="vote-up">UP</span>';
  if (v==='Down') return '<span class="vote-down">DOWN</span>';
  return '<span class="vote-abstain">ABSTAIN</span>';
}
function detailRow(label, value) {
  if (value==null) return '';
  return '<div class="detail-row"><span class="detail-label">'+label+'</span><span class="detail-value">'+value+'</span></div>';
}
function buildDetail(t) {
  let h = '<div class="detail-inner"><div class="detail-grid">';
  h += '<div class="detail-section">Trade Details</div>';
  h += detailRow('Market', t.market_id);
  h += detailRow('Timestamp', t.timestamp);
  h += detailRow('Side', t.side);
  h += detailRow('Entry Odds', t.entry_odds ? fmt(t.entry_odds,3) : '-');
  h += detailRow('Cost', t.position_size ? '$'+fmt(t.position_size) : '-');
  h += detailRow('Payout Rate', t.payout_rate ? (t.payout_rate*100).toFixed(1)+'%' : '-');
  h += detailRow('Risk/Reward', t.risk_reward_ratio ? t.risk_reward_ratio.toFixed(2)+':1' : '-');
  h += detailRow('Confidence', t.confidence_level.toUpperCase());
  h += detailRow('Outcome', t.outcome.toUpperCase());
  h += detailRow('P&L', t.pnl!=null ? '$'+(t.pnl>=0?'+':'')+fmt(t.pnl) : '-');
  h += detailRow('Balance After', t.portfolio_balance_after ? '$'+fmt(t.portfolio_balance_after) : '-');
  const s = t.signals;
  if (s) {
    h += '<div class="detail-section">Price Signals</div>';
    h += detailRow('Chainlink', s.chainlink_price ? '$'+fmt(s.chainlink_price) : '-');
    h += detailRow('Spot', s.spot_price ? '$'+fmt(s.spot_price) : '-');
    if (s.chainlink_spot_divergence!=null) h += detailRow('Divergence', '$'+(s.chainlink_spot_divergence>=0?'+':'')+fmt(s.chainlink_spot_divergence));
    if (s.candle_position_dollars!=null) h += detailRow('Candle Pos', '$'+(s.candle_position_dollars>=0?'+':'')+fmt(s.candle_position_dollars));
    h += '<div class="detail-section">Momentum & Volume</div>';
    if (s.momentum_60s!=null) h += detailRow('Mom 60s', '$'+(s.momentum_60s>=0?'+':'')+fmt(s.momentum_60s,4)+'/s');
    if (s.momentum_120s!=null) h += detailRow('Mom 120s', '$'+(s.momentum_120s>=0?'+':'')+fmt(s.momentum_120s,4)+'/s');
    if (s.cvd!=null) h += detailRow('CVD', (s.cvd>=0?'+':'')+s.cvd.toFixed(6)+' BTC');
    if (s.order_book_ratio!=null) h += detailRow('OB Ratio', fmt(s.order_book_ratio,3));
    h += '<div class="detail-section">Market Structure</div>';
    if (s.liquidation_signal!=null) h += detailRow('Liq Net', '$'+(s.liquidation_signal>=0?'+':'')+fmt(s.liquidation_signal,0));
    if (s.round_number_distance!=null) h += detailRow('Round # Dist', '$'+fmt(s.round_number_distance,0));
    h += detailRow('Regime', s.time_regime||'-');
    h += detailRow('Streak', s.candle_streak||'-');
    h += '<div class="detail-section">Model Votes</div>';
    h += detailRow('Momentum', voteHtml(s.momentum_vote));
    h += detailRow('Reversion', voteHtml(s.reversion_vote));
    h += detailRow('Structure', voteHtml(s.structure_vote));
    h += detailRow('Final', voteHtml(s.final_vote));
  }
  h += '</div></div>';
  return h;
}
function toggleDetail(row) {
  const id = row.dataset.id;
  const detail = document.querySelector('.trade-detail[data-id="'+id+'"]');
  row.classList.toggle('expanded');
  detail.classList.toggle('open');
}
function badge(outcome) {
  const m = {win:'b-win',loss:'b-loss',skip:'b-skip',pending:'b-pending',failed:'b-failed'};
  return '<span class="badge '+(m[outcome]||'')+'">'+ outcome.toUpperCase()+'</span>';
}
async function poll() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();
    const p = d.portfolio;
    const pc = p.pnl >= 0 ? 'pos' : 'neg';
    document.getElementById('portfolio').innerHTML = `
      <div class="balance">$${fmt(p.balance)}</div>
      <div class="pnl ${pc}">${p.pnl>=0?'+':''}$${fmt(p.pnl)} (${p.pnl_pct>=0?'+':''}${fmt(p.pnl_pct,1)}%)</div>
      <div class="stats">
        <div class="row"><span class="lbl">Starting</span><span class="val">$${fmt(p.starting)}</span></div>
        <div class="row"><span class="lbl">Win Rate</span><span class="val">${fmt(p.win_rate,1)}%</span></div>
        <div class="row"><span class="lbl">Trades</span><span class="val">${p.total}</span></div>
        <div class="row"><span class="lbl">Record</span><span class="val"><span class="pos">${p.wins}W</span> / <span class="neg">${p.losses}L</span></span></div>
        <div class="row"><span class="lbl">Skips</span><span class="val">${p.skips} total</span></div>
        <div class="row"><span class="lbl">Best Trade</span><span class="val pos">$+${fmt(p.best_trade)}</span></div>
        <div class="row"><span class="lbl">Worst Trade</span><span class="val neg">$${fmt(p.worst_trade)}</span></div>
        <div class="row"><span class="lbl">Peak Balance</span><span class="val">$${fmt(p.peak_balance)}</span></div>
      </div>`;
    // Preserve expanded rows across polls
    const wasExpanded = new Set();
    document.querySelectorAll('.trade-row.expanded').forEach(r => wasExpanded.add(r.dataset.id));

    let html = '';
    for (const t of d.trades) {
      const isSkip = t.confidence_level === 'skip';
      if (hideSkips && isSkip) continue;
      const slug = t.market_id.replace('btc-updown-5m-','').slice(-8);
      const pnl = t.pnl || 0;
      const pnlStr = isSkip ? '-' : '<span class="'+(pnl>=0?'pos':'neg')+'">$'+(pnl>=0?'+':'')+fmt(pnl)+'</span>';
      const isOpen = wasExpanded.has(String(t.id));
      const rrStr = t.risk_reward_ratio ? t.risk_reward_ratio.toFixed(1)+':1' : '-';
      html += '<tr class="trade-row'+(isOpen?' expanded':'')+'" data-id="'+t.id+'" onclick="toggleDetail(this)">'
        +'<td>'+t.id+'</td><td>'+slug+'</td><td>'+(isSkip?'-':t.side)+'</td>'
        +'<td class="r">'+(t.position_size?'$'+fmt(t.position_size):'-')+'</td>'
        +'<td class="r">'+rrStr+'</td>'
        +'<td>'+(isSkip && t.signals && t.signals.final_vote !== 'ABSTAIN' ? badge('failed') : badge(t.outcome))+'</td><td class="r">'+pnlStr+'</td>'
        +'<td class="r">'+(t.portfolio_balance_after?'$'+fmt(t.portfolio_balance_after):'-')+'</td></tr>';
      html += '<tr class="trade-detail'+(isOpen?' open':'')+'" data-id="'+t.id+'"><td colspan="8">'+buildDetail(t)+'</td></tr>';
    }
    document.getElementById('tbody').innerHTML = html || '<tr><td colspan="8" style="color:var(--dim);text-align:center">No trades</td></tr>';

    // Skip breakdown pie chart
    const sd = p.skip_detail;
    const colorMap = {
      'no_consensus': '#8b949e',
      'risk_blocked': '#f85149',
      'order_rejected': '#f0883e',
      'empty_book': '#d29922',
      'insufficient_liquidity': '#e3b341',
      'price_out_of_range': '#bc8cff',
      'service_unavailable': '#58a6ff',
      'invalid_amount': '#ff7b72',
    };
    const nameMap = {
      'no_consensus': 'No Consensus',
      'risk_blocked': 'Risk Blocked',
      'order_rejected': 'Order Rejected',
      'empty_book': 'Empty Book',
      'insufficient_liquidity': 'Low Liquidity',
      'price_out_of_range': 'Price Out of Range',
      'service_unavailable': 'Service Unavailable',
      'invalid_amount': 'Invalid Amount',
    };
    const skipLabels = []; const skipData = []; const skipColors = [];
    for (const [key, count] of Object.entries(sd)) {
      if (count > 0) {
        skipLabels.push(nameMap[key] || key);
        skipData.push(count);
        skipColors.push(colorMap[key] || '#8b949e');
      }
    }
    const skipCtx = document.getElementById('skipChart').getContext('2d');
    if (skipChart) {
      skipChart.data.labels = skipLabels;
      skipChart.data.datasets[0].data = skipData;
      skipChart.data.datasets[0].backgroundColor = skipColors;
      skipChart.update('none');
    } else if (skipData.length > 0) {
      skipChart = new Chart(skipCtx, {
        type: 'doughnut',
        data: {
          labels: skipLabels,
          datasets: [{ data: skipData, backgroundColor: skipColors, borderWidth: 0 }]
        },
        options: {
          responsive: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: function(ctx) {
                  const total = ctx.dataset.data.reduce((a,b) => a+b, 0);
                  const pct = total > 0 ? Math.round(ctx.raw / total * 100) : 0;
                  return ctx.label + ': ' + ctx.raw + ' (' + pct + '%)';
                }
              }
            }
          }
        }
      });
    }
    // Legend
    const totalSkips = skipData.reduce((a,b) => a+b, 0);
    let legendHtml = '';
    for (let i = 0; i < skipLabels.length; i++) {
      const pct = totalSkips > 0 ? Math.round(skipData[i] / totalSkips * 100) : 0;
      legendHtml += '<div class="row"><span class="lbl"><span style="color:'+skipColors[i]+'">&#9679;</span> '+skipLabels[i]+'</span><span class="val">'+skipData[i]+' ('+pct+'%)</span></div>';
    }
    document.getElementById('skipLegend').innerHTML = legendHtml;

    // Equity curve
    if (d.equity_curve && d.equity_curve.length > 0) {
      const labels = d.equity_curve.map(e => '#' + e.id);
      const balances = d.equity_curve.map(e => e.portfolio_balance_after);
      const colors = d.equity_curve.map(e => e.outcome === 'win' ? '#3fb950' : '#f85149');

      const ctx = document.getElementById('equityChart').getContext('2d');
      if (equityChart) {
        equityChart.data.labels = labels;
        equityChart.data.datasets[0].data = balances;
        equityChart.data.datasets[0].pointBackgroundColor = colors;
        equityChart.update('none');
      } else {
        equityChart = new Chart(ctx, {
          type: 'line',
          data: {
            labels: labels,
            datasets: [{
              label: 'Balance',
              data: balances,
              borderColor: '#39d2c0',
              borderWidth: 2,
              pointBackgroundColor: colors,
              pointRadius: 4,
              pointHoverRadius: 6,
              fill: {
                target: 'origin',
                above: 'rgba(57,210,192,0.08)',
              },
              tension: 0.3,
            }]
          },
          options: {
            responsive: true,
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  label: function(ctx) {
                    const e = d.equity_curve[ctx.dataIndex];
                    const pnl = e.pnl >= 0 ? '+$'+e.pnl.toFixed(2) : '-$'+Math.abs(e.pnl).toFixed(2);
                    return '$' + ctx.parsed.y.toFixed(2) + ' (' + e.outcome.toUpperCase() + ' ' + pnl + ')';
                  }
                }
              }
            },
            scales: {
              x: {
                ticks: { color: '#8b949e', font: { size: 9 } },
                grid: { color: 'rgba(48,54,61,0.5)' },
              },
              y: {
                ticks: {
                  color: '#8b949e',
                  font: { size: 10 },
                  callback: function(v) { return '$' + v.toFixed(0); }
                },
                grid: { color: 'rgba(48,54,61,0.5)' },
              }
            }
          }
        });
      }
    }

    document.getElementById('footer').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) { document.getElementById('footer').textContent = 'Error: ' + e.message; }
}
poll();
setInterval(poll, 5000);
</script>
</body></html>"""


async def handle_index(request):
    return web.Response(text=HTML, content_type="text/html")


async def handle_api(request):
    loop = asyncio.get_event_loop()
    conn = request.app["conn"]
    try:
        state = await loop.run_in_executor(None, partial(query_state, conn))
        return web.json_response(state)
    except Exception as e:
        try:
            request.app["conn"] = get_conn()
        except Exception:
            pass
        return web.json_response({"error": str(e)}, status=500)


async def main():
    conn = get_conn()
    app = web.Application()
    app["conn"] = conn
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", PORT)
    await site.start()
    print(f"Local dashboard running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        conn.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
