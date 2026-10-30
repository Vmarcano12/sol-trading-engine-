const crypto = require('crypto');

const API_KEY = process.env.BINANCE_API_KEY;
const API_SECRET = process.env.BINANCE_API_SECRET;
const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const BOT_SECRET_TOKEN = process.env.BOT_SECRET_TOKEN || 'MiClaveSecretaPro2026';

const FUTURES_BASE = 'https://fapi.binance.com';
const SYMBOL = 'SOLUSDT';
const LEVERAGE = 5;
const MARGIN_PER_TRADE = 25.0;
const TP_PCT = 0.030;
const SL_PCT = 0.015;

const sign = (query) => crypto.createHmac('sha256', API_SECRET).update(query).digest('hex');

async function sendTelegram(msg) {
  if (!TELEGRAM_TOKEN || !CHAT_ID) return;
  try {
    await fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: CHAT_ID, text: msg, parse_mode: 'HTML' })
    });
  } catch (e) {
    console.error('Error Telegram:', e);
  }
}

async function fapi(endpoint, method = 'GET', params = {}) {
  const timestamp = Date.now();
  let queryString = Object.keys(params).map(k => `${k}=${encodeURIComponent(params[k])}`).join('&');
  queryString = queryString ? `${queryString}&timestamp=${timestamp}` : `timestamp=${timestamp}`;
  const signature = sign(queryString);
  const fullUrl = `${FUTURES_BASE}${endpoint}?${queryString}&signature=${signature}`;

  const res = await fetch(fullUrl, {
    method: method,
    headers: {
      'X-MBX-APIKEY': API_KEY,
      'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
      'Content-Type': 'application/json'
    }
  });
  return await res.json();
}

function calculateRSI(closes, period = 14) {
  if (!closes || closes.length < period + 1) return 50;
  let gains = 0;
  let losses = 0;
  for (let i = 1; i <= period; i++) {
    const change = closes[i] - closes[i - 1];
    if (change > 0) gains += change;
    else losses += Math.abs(change);
  }
  let avgGain = gains / period;
  let avgLoss = losses / period;

  for (let i = period + 1; i < closes.length; i++) {
    const change = closes[i] - closes[i - 1];
    if (change > 0) {
      avgGain = (avgGain * (period - 1) + change) / period;
      avgLoss = (avgLoss * (period - 1)) / period;
    } else {
      avgGain = (avgGain * (period - 1)) / period;
      avgLoss = (avgLoss * (period - 1) + Math.abs(change)) / period;
    }
  }
  if (avgLoss === 0) return 100;
  if (avgGain === 0) return 0;
  return 100 - (100 / (1 + (avgGain / avgLoss)));
}

export default async function handler(req, res) {
  const auth = req.query.auth || req.headers['authorization'];
  if (auth !== BOT_SECRET_TOKEN) {
    return res.status(401).json({ status: 'error', message: 'Acceso no autorizado' });
  }

  try {
    // 1. Balance de cuenta
    const fAccount = await fapi('/fapi/v2/account');
    let availableBalance = 0;
    let totalWalletBalance = 0;

    for (const asset of fAccount.assets || []) {
      if (asset.asset === 'USDT') {
        availableBalance = parseFloat(asset.availableBalance);
        totalWalletBalance = parseFloat(asset.walletBalance);
      }
    }

    // 2. Posición abierta
    let openPosition = null;
    for (const pos of fAccount.positions || []) {
      if (pos.symbol === SYMBOL && parseFloat(pos.positionAmt) !== 0) {
        openPosition = {
          amt: parseFloat(pos.positionAmt),
          entryPrice: parseFloat(pos.entryPrice),
          unrealizedProfit: parseFloat(pos.unrealizedProfit),
          leverage: parseInt(pos.leverage)
        };
      }
    }

    // 3. Velas y RSI 1h (con User-Agent y respaldo)
    let klines = [];
    try {
      const resKlines = await fetch(`${FUTURES_BASE}/fapi/v1/klines?symbol=${SYMBOL}&interval=1h&limit=50`, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }
      });
      klines = await resKlines.json();
    } catch (e) {}

    if (!Array.isArray(klines) || klines.length === 0) {
      const resSpot = await fetch(`https://api.binance.com/api/v3/klines?symbol=${SYMBOL}&interval=1h&limit=50`, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' }
      });
      klines = await resSpot.json();
    }

    const closes = klines.map(k => parseFloat(k[4]));
    const currentPrice = closes[closes.length - 1];
    const rsi14 = calculateRSI(closes, 14);

    let actionTaken = 'MONITORING';

    // 4. Gestión de posición activa
    if (openPosition) {
      const isLong = openPosition.amt > 0;
      const entry = openPosition.entryPrice;
      const qty = Math.abs(openPosition.amt).toFixed(2);
      const exitSide = isLong ? 'SELL' : 'BUY';
      const diffPct = isLong ? (currentPrice - entry) / entry : (entry - currentPrice) / entry;
      const pnlUSDT = openPosition.unrealizedProfit;

      if (diffPct >= TP_PCT) {
        await fapi('/fapi/v1/order', 'POST', { symbol: SYMBOL, side: exitSide, type: 'MARKET', quantity: qty, reduceOnly: 'true' });
        actionTaken = `TAKE_PROFIT: +$${pnlUSDT.toFixed(2)} USDT`;
        await sendTelegram(`<b>[CIERRE EN GANANCIA] TAKE PROFIT (SOL)</b>\n\n• Ganancia neta: <b>+$${pnlUSDT.toFixed(2)} USDT</b> (+${(diffPct * 100 * LEVERAGE).toFixed(1)}% ROE)\n• Salida: $${currentPrice}\n• Saldo: <b>$${(totalWalletBalance + pnlUSDT).toFixed(2)} USDT</b>`);
      } else if (diffPct <= -SL_PCT) {
        await fapi('/fapi/v1/order', 'POST', { symbol: SYMBOL, side: exitSide, type: 'MARKET', quantity: qty, reduceOnly: 'true' });
        actionTaken = `STOP_LOSS: -$${Math.abs(pnlUSDT).toFixed(2)} USDT`;
        await sendTelegram(`<b>[PROTECCIÓN DE CAPITAL] STOP LOSS (SOL)</b>\n\n• Pérdida: -$${Math.abs(pnlUSDT).toFixed(2)} USDT\n• Saldo: <b>$${(totalWalletBalance + pnlUSDT).toFixed(2)} USDT</b>`);
      }
    } 
    // 5. Evaluar entrada si no hay posición
    else {
      if (availableBalance >= MARGIN_PER_TRADE) {
        try { await fapi('/fapi/v1/marginType', 'POST', { symbol: SYMBOL, marginType: 'ISOLATED' }); } catch (e) {}
        try { await fapi('/fapi/v1/leverage', 'POST', { symbol: SYMBOL, leverage: LEVERAGE }); } catch (e) {}

        if (rsi14 >= 30 && rsi14 <= 48) {
          const notionalValue = MARGIN_PER_TRADE * LEVERAGE;
          const qty = (Math.floor((notionalValue / currentPrice) * 10) / 10).toFixed(2);

          if (parseFloat(qty) >= 0.1) {
            await fapi('/fapi/v1/order', 'POST', { symbol: SYMBOL, side: 'BUY', type: 'MARKET', quantity: qty });
            actionTaken = `LONG_OPENED: ${qty} SOL`;
            const tpPrice = (currentPrice * (1 + TP_PCT)).toFixed(2);
            const slPrice = (currentPrice * (1 - SL_PCT)).toFixed(2);
            await sendTelegram(`<b>[NUEVA OPERACIÓN EJECUTADA] FUTUROS 5X (SOL)</b>\n\n• Posición: <b>LONG</b>\n• Volumen: <b>${qty} SOL</b> (~$${notionalValue.toFixed(2)})\n• Margen: <b>$${MARGIN_PER_TRADE} USDT</b>\n• RSI 1h: <b>${rsi14.toFixed(1)}</b>\n\n• TP: <b>$${tpPrice}</b> (+3.0% / +$3.75 USDT)\n• SL: <b>$${slPrice}</b> (-1.5% / -$1.87 USDT)`);
          }
        }
      }
    }

    return res.status(200).json({
      status: 'success',
      timestamp: new Date().toISOString(),
      action: actionTaken,
      price: currentPrice,
      rsi14: rsi14.toFixed(2),
      balanceUSDT: totalWalletBalance,
      availableUSDT: availableBalance,
      openPosition: openPosition
    });

  } catch (error) {
    return res.status(500).json({ status: 'error', message: error.message });
  }
}
