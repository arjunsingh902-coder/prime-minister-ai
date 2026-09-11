# ============================================================
# DASHBOARD / HTML FRONTEND 
# ============================================================

HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prime Minister AI (Pro)</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; background: #07111f; color: #e8eef7; font-family: Inter, system-ui, sans-serif; }
header { padding: 18px; border-bottom: 1px solid #1b2a3e; background: #091625; }
h1 { margin: 0; font-size: 23px; }
.subtitle { margin-top: 5px; color: #8294aa; font-size: 13px; }
.container { max-width: 1200px; margin: auto; padding: 16px; }
.topbar { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 10px; margin-bottom: 15px; }
.panel { background: #0b1828; border: 1px solid #1b2a3e; border-radius: 14px; padding: 14px; }
.label { color: #7e91a8; font-size: 12px; }
.value { font-size: 20px; font-weight: 700; margin-top: 4px; }
.green { color: #45e09b; }
.red { color: #ff6577; }
.yellow { color: #ffc857; }
.gray { color: #91a0b5; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; }
button, select, input { background: #101f32; color: white; border: 1px solid #2a405a; border-radius: 9px; padding: 10px 12px; font-size: 14px; cursor: pointer; }
button:hover { background: #172b43; }
.coin-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(210px,1fr)); gap: 12px; }
.coin { cursor: pointer; transition: .15s; }
.coin:hover { transform: translateY(-2px); border-color: #52759d; }
.coin.on { border-color: #ffc857; box-shadow: 0 0 10px rgba(255, 200, 87, 0.2); }
.coin-title { display: flex; justify-content: space-between; align-items: center; }
.coin-name { font-size: 17px; font-weight: 700; display: flex; align-items: center;}
.setup-dot { width: 10px; height: 10px; border-radius: 50%; margin-left: 8px; display: inline-block; animation: pulse 2s infinite; }
.dot-green { background-color: #45e09b; box-shadow: 0 0 8px #45e09b; }
.dot-red { background-color: #ff6577; box-shadow: 0 0 8px #ff6577; }
.dot-none { background-color: transparent; animation: none; }
@keyframes pulse { 0% { transform: scale(0.95); opacity: 0.8; } 50% { transform: scale(1.1); opacity: 1; } 100% { transform: scale(0.95); opacity: 0.8; } }
.badge { font-size: 11px; padding: 4px 8px; border-radius: 999px; background: #16263a; }
.badge-armed { background: #ffc857; color: #000; font-weight: bold; }
.signal { font-size: 21px; font-weight: 800; margin: 15px 0 4px; }
.price { font-size: 15px; }
.reason { margin-top: 10px; color: #8294aa; font-size: 12px; min-height: 32px; }
.meta { margin-top: 12px; display: flex; justify-content: space-between; color: #9aacbf; font-size: 12px; }
.section { margin-top: 16px; }
.trade-box { display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: 10px; }
.event-list { max-height: 250px; overflow: auto; }
.event-item { padding: 8px 0; border-bottom: 1px solid #18273a; font-size: 12px; color: #a9b8ca; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: none; align-items: center; justify-content: center; padding: 15px; z-index: 50; }
.modal.show { display: flex; }
.modal-box { width: 100%; max-width: 480px; max-height: 90vh; overflow: auto; background: #0b1828; border: 1px solid #2a405a; border-radius: 16px; padding: 18px; }
.modal-header { display: flex; justify-content: space-between; align-items: center; }
.form-row { margin-top: 12px; }
.form-row label { display: block; color: #8ea0b4; font-size: 12px; margin-bottom: 5px; }
.form-row input, .form-row select { width: 100%; }
.rule { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid #18273a; font-size: 12px; }
.rule span:first-child { color: #8497ac; }
.small { color: #71849a; font-size: 11px; }
.margin-calc { background: #16263a; padding: 10px; border-radius: 8px; margin-top: 15px; font-size: 14px; font-weight: bold; text-align: center; color: #ffc857; }
</style>
</head>
<body>

<header>
    <h1>Prime Minister AI (Pro Sniper)</h1>
    <div class="subtitle">Multi-Timeframe Trigger & Email Alert System</div>
</header>

<div class="container">
    <div class="topbar">
        <div class="panel"><div class="label">PUBLIC IP</div><div id="public_ip" class="value yellow">--</div></div>
        <div class="panel"><div class="label">ENGINE SYSTEM</div><div id="system" class="value gray">OFF</div></div>
        <div class="panel"><div class="label">MODE</div><div id="mode" class="value">PAPER</div></div>
        <div class="panel"><div class="label">ARMED COIN</div><div id="selected" class="value">None</div></div>
    </div>

    <div class="panel">
        <div class="controls">
            <button onclick="systemToggle()">SYSTEM ON / OFF</button>
            <button onclick="setMode('PAPER')">PAPER</button>
            <button onclick="setMode('LIVE')">LIVE</button>
            <button onclick="manualClose()">CLOSE CURRENT TRADE</button>
        </div>
    </div>
    
    <div class="section">
        <div class="panel">
            <div class="label">CONNECTIONS & HEALTH</div>
            <div id="connections" style="margin-top:12px"></div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">TOP 5 MARKET WATCH (Click to ARM Sniper)</div>
            <div id="coins" class="coin-grid" style="margin-top:12px"></div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">ACTIVE TRADE / TRAILING DATA</div>
            <div id="trade" style="margin-top:12px">None</div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">EVENTS / ALERTS LOG</div>
            <div id="events" class="event-list" style="margin-top:12px"></div>
        </div>
    </div>
</div>

<div id="modal" class="modal">
    <div class="modal-box">
        <div class="modal-header">
            <div>
                <div id="modalTitle" style="font-size:20px; font-weight:800;"></div>
                <div id="modalSubtitle" class="small"></div>
            </div>
            <button onclick="closeModal()">X</button>
        </div>
        <div id="rules" style="margin-top:14px"></div>
        <div class="form-row">
            <label>Quantity (Lots/Contracts) <span id="coinEquivalent" style="color:#ffc857; font-weight:bold; margin-left: 10px;">--</span></label>
            <input id="quantity" type="number" step="1" oninput="calculateMargin()" />
        </div>
        <div class="form-row"><label>Leverage</label><select id="leverage" onchange="calculateMargin()"></select></div>
        <div id="marginDisplay" class="margin-calc">Estimated Margin Required: -- USD</div>
        <div class="controls" style="margin-top:16px">
            <button onclick="saveSettings()">SAVE SETTINGS</button>
            <button onclick="toggleSelectedCoin()">ARM / DISARM</button>
        </div>
    </div>
</div>

<script>
let appState = null;
let selectedModalCoin = null;
let currentModalRules = null;
let lastNotifiedSignals = { error: null };

if (Notification.permission !== "granted" && Notification.permission !== "denied") {
    Notification.requestPermission();
}

function money(value) {
    if (value === null || value === undefined) return "--";
    const n = Number(value);
    if (!Number.isFinite(n)) return "--";
    return n >= 1000 ? n.toLocaleString(undefined, {maximumFractionDigits: 2}) : n.toLocaleString(undefined, {maximumFractionDigits: 8});
}

function signalClass(side) {
    if (side === "LONG") return "green";
    if (side === "SHORT") return "red";
    return "gray";
}

function calculateMargin() {
    if(!appState || !selectedModalCoin || !currentModalRules) return;
    const qty = Number(document.getElementById("quantity").value);
    const lev = Number(document.getElementById("leverage").value);
    const cv = currentModalRules.contract_value || 1;
    
    // 1. Calculate and show Equivalent Coin Amount (Always works, price not needed)
    if(qty > 0) {
        const symbolBase = selectedModalCoin.replace("USD", "");
        document.getElementById("coinEquivalent").innerText = `(Equivalent to ${(qty * cv).toFixed(4)} ${symbolBase})`;
    } else {
        document.getElementById("coinEquivalent").innerText = `--`;
    }

    // 2. Calculate Dollar Margin (Needs Live Price)
    const price = appState.coins[selectedModalCoin].last_signal.price;
    if(qty > 0 && lev > 0 && price > 0) {
        const notional = qty * cv * price;
        const pure_margin = notional / lev;
        const delta_buffer = notional * 0.0015; 
        const total_margin = pure_margin + delta_buffer;
        
        document.getElementById("marginDisplay").innerText = `Estimated Margin Required: ~$${total_margin.toFixed(2)}`;
    } else {
        document.getElementById("marginDisplay").innerText = `Estimated Margin Required: Waiting for live price...`;
    }
}

function checkAndNotify(state) {
    if (state.runtime.last_scan_error) {
        if (lastNotifiedSignals["error"] !== state.runtime.last_scan_error) {
            lastNotifiedSignals["error"] = state.runtime.last_scan_error;
            if (Notification.permission === "granted") new Notification("⚠️ Error Alert", { body: state.runtime.last_scan_error });
        }
    } else { lastNotifiedSignals["error"] = null; }

    for (const symbol of ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]) {
        const signal = state.coins[symbol]?.last_signal;
        if (!signal) continue;
        if (signal.score >= 75 && signal.side !== "NO_TRADE") {
            if (lastNotifiedSignals[symbol] !== signal.side) {
                lastNotifiedSignals[symbol] = signal.side;
                if (Notification.permission === "granted") {
                    new Notification(`🚀 Setup Ready: ${symbol}`, { body: `Target Breakout Price: ${signal.trigger_price.toFixed(2)}` });
                }
            }
        } else { lastNotifiedSignals[symbol] = null; }
    }
}

function render(state) {
    appState = state;
    checkAndNotify(state);

    document.getElementById("public_ip").innerText = state.runtime.public_ip || "Loading...";
    const system = document.getElementById("system");
    system.innerText = state.system_on ? "ON" : "OFF";
    system.className = "value " + (state.system_on ? "green" : "gray");
    document.getElementById("mode").innerText = state.mode;
    document.getElementById("selected").innerText = state.selected_coin || "None";
    
    document.getElementById("connections").innerHTML = `
        <div class="rule"><span>Delta API Status</span><span class="${state.runtime.last_scan_error ? 'red' : 'green'}">${state.runtime.last_scan_error ? 'CONNECTION ERROR' : 'CONNECTED & READY'}</span></div>
        <div class="rule"><span>Delta Error Detail</span><span class="${state.runtime.last_scan_error ? 'red' : 'gray'}">${state.runtime.last_scan_error ? state.runtime.last_scan_error : 'None'}</span></div>
        <div class="rule"><span>CoinMarketCap API</span><span class="${state.cmc_connected ? 'green' : 'red'}">${state.cmc_connected ? 'CONNECTED' : (state.cmc_error ? state.cmc_error : 'NOT CONNECTED')}</span></div>
        <div class="rule"><span>Background Scanner</span><span class="${state.engine_running ? 'green' : 'red'}">${state.engine_running ? 'SCANNING LIVE' : 'STOPPED/LOADING'}</span></div>
    `;

    const coins = document.getElementById("coins");
    coins.innerHTML = "";

    for (const symbol of ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]) {
        const coin = state.coins[symbol];
        const signal = coin.last_signal || {};
        const armed = coin.armed_signal;

        let dotClass = "dot-none";
        let dotTitle = "No Setup";
        if (signal.score >= 75) {
            dotClass = signal.side === "LONG" ? "dot-green" : "dot-red";
            dotTitle = `${signal.side} Setup Ready!`;
        }

        let badgeHtml = coin.enabled ? `<div class="badge badge-armed">ARMED</div>` : `<div class="badge">OFF</div>`;
        let statusDisplay = "";

        if (armed) {
            statusDisplay = `<div class="yellow" style="font-size:12px; margin-top:5px;">⏳ Waiting to break: <b>${armed.trigger_price.toFixed(2)}</b></div>`;
        } else {
            let trg = signal.trigger_price ? `Breakout lvl: ${signal.trigger_price.toFixed(2)}` : "";
            statusDisplay = `<div class="gray" style="font-size:12px; margin-top:5px;">${trg}</div>`;
        }

        const div = document.createElement("div");
        div.className = "panel coin " + (coin.enabled ? "on" : "");
        div.onclick = () => openCoin(symbol);
        div.innerHTML = `
            <div class="coin-title">
                <div class="coin-name">${symbol} <span class="setup-dot ${dotClass}" title="${dotTitle}"></span></div>
                ${badgeHtml}
            </div>
            <div class="signal ${signalClass(signal.side)}">${signal.side || "NO_TRADE"}</div>
            <div class="price">${money(signal.price)}</div>
            ${statusDisplay}
            <div class="reason">${signal.reason || ""}</div>
            <div class="meta">
                <span>Score: ${signal.score ?? 0}</span>
                <span>Qty: ${coin.quantity} Lots</span>
                <span>${coin.leverage}x</span>
            </div>
        `;
        coins.appendChild(div);
    }
    renderTrade(state);
    renderEvents(state);
}

function renderTrade(state) {
    const box = document.getElementById("trade");
    const trade = state.current_trade;
    if (!trade) { box.innerHTML = `<span class="gray">No active trade</span>`; return; }

    box.innerHTML = `
        <div class="trade-box">
            <div><div class="label">SYMBOL</div><div class="value">${trade.symbol}</div></div>
            <div><div class="label">SIDE</div><div class="value ${signalClass(trade.side)}">${trade.side}</div></div>
            <div><div class="label">ENTRY</div><div class="value">${money(trade.entry)}</div></div>
            <div><div class="label">TRAILING STOP</div><div class="value yellow">${money(trade.stop)}</div></div>
            <div><div class="label">QTY (LOTS)</div><div class="value">${trade.quantity}</div></div>
            <div><div class="label">LEVERAGE</div><div class="value">${trade.leverage}x</div></div>
        </div>
    `;
}

function renderEvents(state) {
    const box = document.getElementById("events");
    box.innerHTML = "";
    for (const item of (state.events || [])) {
        const div = document.createElement("div");
        div.className = "event-item";
        if (item.message.toLowerCase().includes("error") || item.message.toLowerCase().includes("failed") || item.message.includes("http")) div.style.color = "#ff6577";
        else if (item.message.includes("Trailing Stop")) div.style.color = "#ffc857";
        else if (item.message.includes("EXECUTED") || item.message.includes("ARMED") || item.message.includes("Started")) div.style.color = "#45e09b";
        else if (item.message.includes("EXPIRED") || item.message.includes("DISARMED") || item.message.includes("Paused")) div.style.color = "#8294aa";
        else if (item.message.includes("Email")) div.style.color = "#42a5f5";
        
        div.innerText = `${item.time.split('T')[1].slice(0,8)} — ${item.message}`;
        box.appendChild(div);
    }
}

async function loadState() {
    try { const res = await fetch("/api/state", {cache: "no-store"}); render(await res.json()); } 
    catch (e) { console.error(e); }
}

async function systemToggle() {
    if (!appState) return;
    const res = await fetch("/api/system", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: !appState.system_on}) });
    const data = await res.json();
    if (!data.success) alert(data.error);
    await loadState();
}

async function setMode(mode) {
    const res = await fetch("/api/mode", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode}) });
    const data = await res.json();
    if (!data.success) alert(data.error);
    await loadState();
}

async function manualClose() {
    const res = await fetch("/api/trade/close", { method: "POST" });
    const data = await res.json();
    if (!data.success) alert(data.error);
    await loadState();
}

async function openCoin(symbol) {
    selectedModalCoin = symbol;
    const coin = appState.coins[symbol];
    document.getElementById("modalTitle").innerText = symbol;
    document.getElementById("modalSubtitle").innerText = "Delta product rules";
    document.getElementById("quantity").value = coin.quantity;
    
    document.getElementById("modal").classList.add("show");
    
    currentModalRules = { contract_value: 1 };
    calculateMargin();
    
    await loadRules(symbol);
}

async function loadRules(symbol) {
    try {
        const res = await fetch(`/api/coin/${symbol}/rules`, {cache: "no-store"});
        const data = await res.json();
        if (!data.success) { 
            document.getElementById("rules").innerHTML = `<div class="red">${data.error}</div>`; 
            return; 
        }
        currentModalRules = data.rules;
        renderRules(data.rules);
        calculateMargin();
    } catch (e) { 
        document.getElementById("rules").innerHTML = `<div class="red">Failed to load Delta rules</div>`;
    }
}

function renderRules(rules) {
    const box = document.getElementById("rules");
    const maxRaw = rules.max_leverage ?? rules.default_leverage ?? 100;
    const max = Number.isFinite(Number(maxRaw)) ? Number(maxRaw) : 100;
    const min = Number.isFinite(Number(rules.min_leverage)) ? Number(rules.min_leverage) : 1;

    box.innerHTML = `
        <div class="rule"><span>Contract Value</span><span>${rules.contract_value ?? "--"}</span></div>
        <div class="rule"><span>Min qty</span><span>${rules.min_quantity ?? "--"} Lots</span></div>
        <div class="rule"><span>Max qty</span><span>${rules.max_quantity ?? "--"} Lots</span></div>
        <div class="rule"><span>Step</span><span>${rules.quantity_step ?? "--"} Lots</span></div>
        <div class="rule"><span>Max lev</span><span>${max}x</span></div>
    `;

    const select = document.getElementById("leverage");
    select.innerHTML = "";
    
    const common = [1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 125, 150, 200];
    const values = common.filter(v => v >= min && v <= max);
    if (!values.includes(min)) values.unshift(min);
    if (!values.includes(max)) values.push(max);

    for (const value of values) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.innerText = `${value}x`;
        if (appState.coins[selectedModalCoin].leverage == value) opt.selected = true;
        select.appendChild(opt);
    }
}

function closeModal() { document.getElementById("modal").classList.remove("show"); }

async function saveSettings() {
    if (!selectedModalCoin) return;
    const quantity = Number(document.getElementById("quantity").value);
    const leverage = Number(document.getElementById("leverage").value);
    const res = await fetch(`/api/coin/${selectedModalCoin}/settings`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({quantity, leverage}) });
    const data = await res.json();
    if (!data.success) alert(data.error);
    else { closeModal(); await loadState(); }
}

async function toggleSelectedCoin() {
    if (!selectedModalCoin) return;
    const current = appState.coins[selectedModalCoin].enabled;
    const res = await fetch(`/api/coin/${selectedModalCoin}/toggle`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: !current}) });
    const data = await res.json();
    if (!data.success) alert(data.error);
    else { closeModal(); await loadState(); }
}

loadState();
setInterval(loadState, 5000);
</script>
</body>
</html>
"""
