// Execute the real homepage script with a minimal DOM and mocked HTTP/timers.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('web/templates/index.html', 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
let code = scripts.at(-1)[1].replace(/loadTodos\(\);\s*$/, '');
const elements = new Map();
function element(id) {
  return {id, style: {}, classList: {add() {}, remove() {}}, value: '',
    setAttribute() {}, addEventListener() {}, disabled: false,
    set innerHTML(value) {
      if (id === 'todo-list') {
        for (const key of [...elements.keys()]) if (/^(btn|row|widget|ring|pct|step|fail)-/.test(key)) elements.delete(key);
        for (const match of value.matchAll(/id="([^"]+)"/g)) elements.set(match[1], element(match[1]));
      }
    },
    set outerHTML(value) { elements.delete(id); }
  };
}
for (const id of ['search', 'business-filter', 'decision-filter', 'summary', 'todo-list', 'credential-form']) elements.set(id, element(id));
let snapshot = {};
let timerCount = 0;
let posts = 0;
const context = vm.createContext({
  document: {getElementById: id => elements.get(id) || null},
  fetch: async (url, options) => {
    if (options?.method === 'POST') posts++;
    return {ok: true, json: async () => snapshot};
  },
  setInterval: () => ++timerCount, clearInterval() {}, console, alert() {},
});
vm.runInContext(code, context);
async function run(source) { await vm.runInContext(`(async () => { ${source} })()`, context); }
(async () => {
  snapshot = {'1': {status: 'running', progress: 70, step: 'OCR'}};
  await run(`todoItems = [{todoId:'1', name:'路胜', hasResult:false}, {todoId:'2', name:'另一家', hasResult:false}]; renderTodos(todoItems); await refreshAllStatus();`);
  assert.equal(elements.get('widget-1').style.display, 'flex');
  elements.get('search').value = '另一家';
  await run(`filterTodos(); await refreshAllStatus();`);
  assert.equal(elements.has('btn-1'), false);
  assert.equal(vm.runInContext('pollTimer !== null', context), true, 'hidden running task must keep polling');
  elements.get('search').value = '';
  await run(`filterTodos();`);
  assert.equal(elements.get('widget-1').style.display, 'flex');
  assert.equal(elements.get('btn-1').disabled, true);
  assert.equal(elements.get('pct-1').textContent, '70%');
  assert.equal(posts, 0, 'filtering must not restart approval');
  snapshot = {'1': {status:'error', error:'登录过期'}};
  await run(`await refreshAllStatus();`);
  assert.equal(elements.get('btn-1').style.display, '');
  assert.equal(elements.get('btn-1').disabled, false);
  snapshot = {'1': {status:'done'}};
  await run(`await refreshAllStatus(); renderTodos(todoItems);`);
  assert.equal(elements.has('btn-1'), false, 'done state must survive rerender');
  const detail = fs.readFileSync('web/templates/detail.html', 'utf8');
  let detailCode = [...detail.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
  detailCode = detailCode.split('(async function init()')[0].replace('{{ todo_id | tojson }}', '"1"');
  for (const id of ['report-area', 'error-area', 'progress-area', 'step-text', 'error-msg']) elements.set(id, element(id));
  let reports = 0;
  let status = {status:'running', step:'OCR'};
  const detailContext = vm.createContext({
    document: {getElementById: id => elements.get(id), createElement: () => element('retry')},
    confirm: () => true, console, setInterval: () => 1, clearInterval() {},
    fetch: async (url) => url.startsWith('/api/approve/')
      ? {ok:false, status:409} : {ok:true, json:async () => status},
  });
  elements.get('error-msg').appendChild = retry => { elements.set('retry', retry); };
  vm.runInContext(detailCode, detailContext);
  detailContext.reportLoaded = () => reports++;
  vm.runInContext('loadReport = reportLoaded', detailContext);
  await vm.runInContext('reRunApprove()', detailContext);
  assert.equal(vm.runInContext('pollTimer', detailContext), 1, '409 must resume polling');
  status = {status:'error', step:'download_failed', error:'下载失败'};
  await vm.runInContext('pollStatus()', detailContext);
  assert.equal(elements.get('retry').textContent, '重新审批');
  status = {status:'done'};
  await vm.runInContext('reRunApprove()', detailContext);
  assert.equal(reports, 1, 'retry completion must load report');
  console.log('frontend status regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
