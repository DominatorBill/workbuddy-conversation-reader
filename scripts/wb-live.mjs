#!/usr/bin/env node
// wb-live —— 通过 WorkBuddy 的 CDP 调试端口（127.0.0.1:9222）读界面当前状态。
//
// 能力边界（实测结论，别当成能读全部历史）：
//   能  —— 侧边栏任务清单（标题/状态/时间）、当前打开会话的完整正文、页面里任意 JS 状态
//   不能 —— 其他会话的正文：未打开的会话内容不在渲染进程里，只能切过去才读得到
//   历史正文请用 scripts/wb-conv.py（直接读 jsonl，零副作用）
//
// 本脚本只做读取，不点击、不输入、不导航。
//
// 用法：
//   node scripts/wb-live.mjs targets
//   node scripts/wb-live.mjs sidebar [--json]
//   node scripts/wb-live.mjs now [--chars 4000]
//   node scripts/wb-live.mjs eval --unsafe-eval "<js 表达式>"

const PORT = Number(process.env.WB_CDP_PORT || 9222);

async function listTargets() {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  if (!res.ok) throw new Error(`CDP 返回 ${res.status}`);
  return res.json();
}

function pickMain(list) {
  const page = list.find(t =>
    t.type === 'page' &&
    t.url.includes('renderer/index.html') &&
    !t.url.includes('windowKind=')
  );
  if (!page) {
    throw new Error('没找到主窗口。当前 target：' + list.map(t => `${t.type}:${t.title}`).join(', '));
  }
  return page;
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => reject(new Error('WebSocket 连接超时')), 10000);
    ws.onopen = () => { clearTimeout(timer); resolve(ws); };
    ws.onerror = () => { clearTimeout(timer); reject(new Error('WebSocket 连接失败（9222 可能已关闭）')); };
  });
}

let seq = 0;
function send(ws, method, params = {}) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`CDP 超时: ${method}`)), 30000);
    const onMsg = ev => {
      let d;
      try { d = JSON.parse(ev.data); } catch { return; }
      if (d.id !== id) return;
      clearTimeout(timer);
      ws.removeEventListener('message', onMsg);
      d.error ? reject(new Error(JSON.stringify(d.error))) : resolve(d.result);
    };
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(ws, expression) {
  const r = await send(ws, 'Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
    timeout: 25000,
  });
  if (r.exceptionDetails) {
    const d = r.exceptionDetails;
    throw new Error(d.exception?.description || d.text || '表达式执行失败');
  }
  return r.result?.value;
}

// ---- 页面内取数表达式 ----
const EXPR_SIDEBAR = `(() => {
  const pick = sel => { const e = document.querySelector(sel); return e ? e.innerText : null; };
  const shell = document.querySelector('[data-testid="conversation-shell"]');
  // 侧边栏是 body 内除对话区以外的部分，用已知 testid 划界
  const full = document.body.innerText || '';
  const navEnd = shell ? full.indexOf(shell.innerText.slice(0, 40)) : -1;
  const nav = navEnd > 0 ? full.slice(0, navEnd) : full.slice(0, 2000);
  const items = [...document.querySelectorAll('a,button,[role="button"],[role="link"]')]
    .map(e => (e.innerText || '').trim())
    .filter(t => t.length > 1 && t.length < 80 && /\\d/.test(t) === false);
  return JSON.stringify({ navText: nav, candidates: [...new Set(items)].slice(0, 120) });
})()`;

const EXPR_NOW = chars => `(() => {
  const shell = document.querySelector('[data-testid="conversation-shell"]');
  const top = document.querySelector('[data-testid="conversation-topbar"]');
  const t = shell ? shell.innerText : '';
  return JSON.stringify({
    title: top ? top.innerText.split('\\n')[0] : null,
    length: t.length,
    text: t.slice(-${chars})
  });
})()`;

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  if (!cmd) {
    console.log('用法: node wb-live.mjs <targets|sidebar|now|eval> [参数]');
    process.exit(2);
  }
  if (cmd === 'eval' && !rest.includes('--unsafe-eval')) {
    console.error('eval 可执行任意页面 JavaScript。明确启用请传 --unsafe-eval；此命令不属于只读查询接口。');
    process.exit(2);
  }

  const list = await listTargets();

  if (cmd === 'targets') {
    console.log(`CDP 端口 ${PORT} 可连 target ${list.length} 个：`);
    for (const t of list) {
      const tag = t.url.includes('windowKind=') ? (t.url.match(/windowKind=([a-z-]+)/) || [])[1] : 'main';
      console.log(`  [${t.type}] ${t.title}  (${tag})`);
      console.log(`      ${t.webSocketDebuggerUrl}`);
    }
    return;
  }

  const target = pickMain(list);
  const ws = await connect(target.webSocketDebuggerUrl);
  await send(ws, 'Runtime.enable');

  try {
    if (cmd === 'sidebar') {
      const raw = await evaluate(ws, EXPR_SIDEBAR);
      const d = JSON.parse(raw);
      if (rest.includes('--json')) {
        console.log(JSON.stringify(d, null, 1));
      } else {
        console.log('=== 侧边栏文本 ===');
        console.log(d.navText.trim());
      }
    } else if (cmd === 'now') {
      const i = rest.indexOf('--chars');
      const chars = i >= 0 ? Number(rest[i + 1]) : 4000;
      const d = JSON.parse(await evaluate(ws, EXPR_NOW(chars)));
      console.log(`任务: ${d.title}`);
      console.log(`正文总长: ${d.length} 字（显示最后 ${chars} 字）`);
      console.log('---');
      console.log(d.text);
    } else if (cmd === 'eval') {
      const expr = rest.filter(x => x !== '--unsafe-eval').join(' ');
      if (!expr) { console.error('缺表达式'); process.exit(2); }
      const v = await evaluate(ws, expr);
      console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 1));
    } else {
      console.error(`未知子命令: ${cmd}`);
      process.exit(2);
    }
  } finally {
    ws.close();
  }
}

main().catch(e => {
  console.error('错误:', e.message);
  process.exit(1);
});
