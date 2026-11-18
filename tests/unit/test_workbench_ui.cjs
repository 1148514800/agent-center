'use strict';

const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const {join} = require('node:path');

const web = join(__dirname, '../../src/agent_center/web');
const html = readFileSync(join(web, 'index.html'), 'utf8');
const css = readFileSync(join(web, 'style.css'), 'utf8');
const script = readFileSync(join(web, 'app.js'), 'utf8');

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.attributes = new Map();
    this.children = [];
    this.listeners = {};
    this.classes = new Set();
    this.classList = {
      contains: name => this.classes.has(name),
      remove: name => this.classes.delete(name),
      toggle: name => this.classes.has(name) ? this.classes.delete(name) : this.classes.add(name),
    };
    this.style = new Map();
    this.style.setProperty = this.style.set.bind(this.style);
    this.value = '';
    this.scrollTop = 0;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  click() { return this.onclick(); }
  focus() { this.focused = true; }
  setPointerCapture(id) { this.captured = id; }
  key(key) {
    let handled = false;
    this.listeners.keydown({key, preventDefault() { handled = true; }});
    assert.ok(handled, key);
  }
}

const task = id => ({task_id: id, goal: id, status: 'cancelled', created_at: '2026-11-18T00:00:00Z'});

async function workbench({protectedId} = {}) {
  const elements = new Map();
  for (const [, tag, attrs] of html.matchAll(/<(\w+)\s+([^>]+)>/g)) {
    const id = /\bid="([^"]+)"/.exec(attrs)?.[1];
    if (!id) continue;
    const element = new Element(tag);
    for (const [, name, value] of attrs.matchAll(/([\w-]+)="([^"]*)"/g)) {
      element.setAttribute(name, value);
    }
    elements.set('#' + id, element);
  }
  const get = selector => elements.get(selector);
  const shell = new Element('div');
  const conversation = new Element('section');
  const composer = new Element('div');
  elements.set('.shell', shell);
  elements.set('.conversation', conversation);
  elements.set('.composer-wrap', composer);
  elements.set('meta[name="agent-center-token"]', {content: 'synthetic-token'});
  shell.clientWidth = 1440;
  conversation.clientHeight = 700;
  get('#tasks').clientHeight = get('#chats').clientHeight = 200;
  get('#reading-split').offsetHeight = 14;
  Object.defineProperty(get('#prompt'), 'offsetHeight', {
    get: () => parseInt(conversation.style.get('--draft-height') ?? '62px', 10),
  });
  let composerControlsHeight = 180;
  Object.defineProperty(composer, 'scrollHeight', {
    get: () => composerControlsHeight + get('#prompt').offsetHeight,
  });

  const requests = [], confirmations = [], held = new Map();
  const data = {api_revision: 4, tasks: [task('first'), task('second')],
    conversations: [{conversation_id: 'chat', title: 'Saved chat'}], limits: {}, agents: []};
  const chat = {conversation_id: 'chat', messages: [{text: 'A separate chat', task_id: 'second'}]};
  let confirmed = true, poll, onResize;
  vm.runInNewContext(script, {
    document: {querySelector: get, querySelectorAll: () => [], createTextNode: text => text,
      createElement: tag => new Element(tag), addEventListener() {}},
    ResizeObserver: class { constructor(callback) { onResize = callback; } observe() {} },
    requestAnimationFrame: fn => fn(),
    setInterval: fn => { poll = fn; },
    setTimeout() {},
    clearTimeout() {},
    confirm: question => { confirmations.push(question); return confirmed; },
    fetch: async (path, options) => {
      requests.push([path, options.method, options.body && JSON.parse(options.body)]);
      let result;
      if (path === '/api/state') result = data;
      else if (path === '/api/conversations/chat') result = chat;
      else {
        const match = /^\/api\/tasks\/([^/]+)(\/delete)?$/.exec(path);
        assert.ok(match, 'Unexpected fake request: ' + path);
        const [, id, remove] = match;
        const entry = data.tasks.find(task => task.task_id === id);
        if (!entry) result = {error: '任务不存在'};
        else if (!remove) result = {task: entry, messages: [{text: id}], events: [],
          plan: [], runs: [], approvals: [], recovery: {}};
        else if (id === protectedId) result = {error: '请先停止 / 取消运行并对账，再删除任务'};
        else {
          data.tasks = data.tasks.filter(task => task.task_id !== id);
          if (chat.messages[0].task_id === id) chat.messages[0].task_id = null;
          result = {deleted: id};
        }
      }
      const snapshot = structuredClone(result);
      if (held.has(path)) {
        const pending = held.get(path);
        held.delete(path);
        await pending;
      }
      return {ok: !snapshot.error, json: async () => snapshot};
    },
  }, {filename: 'app.js'});
  await new Promise(setImmediate);

  const row = index => get('#tasks').children[index];
  return {get, requests, confirmations, shell, conversation, composer, poll, onResize,
    setConfirmed: value => { confirmed = value; },
    setControlsHeight: height => { composerControlsHeight = height; },
    open: index => row(index).children[0].click(),
    remove: index => row(index).children[1].click(),
    hold(path) {
      let release;
      held.set(path, new Promise(resolve => { release = resolve; }));
      return release;
    },
  };
}

test('sidebar delete has a separate button, confirms, and works without opening a task', async () => {
  const page = await workbench();
  const [open, remove] = page.get('#tasks').children[0].children;
  assert.equal(open.tagName, 'button');
  assert.equal(remove.tagName, 'button');
  assert.equal(remove.textContent, '删除');
  assert.equal(remove.getAttribute('aria-label'), '删除任务：first');
  assert.equal(page.get('#chats').children[0].children.length, 0);
  page.requests.length = 0;
  page.setConfirmed(false);
  await remove.click();
  assert.match(page.confirmations[0], /工作区文件会保留.*停止并对账/);
  assert.deepEqual(page.requests, []);
  assert.equal(page.get('#tasks').children.length, 2);

  page.setConfirmed(true);
  await remove.click();
  assert.deepEqual(page.requests, [
    ['/api/tasks/first/delete', 'POST', {confirmation: 'first'}],
    ['/api/state', 'GET', undefined],
  ]);
  assert.equal(page.get('#task-count').textContent, 1);
  assert.equal(page.get('#tasks').children[0].children[0].children[0].textContent, 'second');
  assert.equal(page.get('#welcome').hidden, false);
});

test('deleting the selected task clears the thread, controls, and inspector', async () => {
  const page = await workbench();
  await page.open(0);
  assert.equal(page.get('#page-title').textContent, 'first');
  const priorDetails = page.get('#detail-content').children[0];
  await page.remove(0);
  assert.equal(page.get('#thread').hidden, true);
  assert.equal(page.get('#thread').children.length, 0);
  assert.equal(page.get('#task-controls').hidden, true);
  assert.equal(page.get('#welcome').hidden, false);
  assert.equal(page.get('#workspace-button').title, '');
  assert.notEqual(page.get('#detail-content').children[0], priorDetails);
  assert.match(page.get('#detail-content').children[0].textContent, /创建或选择任务后/);
  assert.equal(page.get('#tasks').children.length, 1);
});

test('deleting another task keeps the selected task and its thread', async () => {
  const page = await workbench();
  await page.open(0);
  const message = page.get('#thread').children[0];
  await page.remove(1);
  assert.equal(page.get('#page-title').textContent, 'first');
  assert.equal(page.get('#thread').children[0], message);
  assert.equal(page.get('#tasks').children[0].children[0].className, 'task-item selected');
  assert.equal(page.get('#task-controls').hidden, false);
});

test('a detail delete uses the same endpoint and keeps a task opened while deletion is pending', async () => {
  const page = await workbench();
  await page.open(0);
  const acknowledge = page.hold('/api/tasks/first/delete');
  const deletion = page.get('#delete-task').click();
  await page.open(1);
  acknowledge();
  await deletion;
  assert.ok(page.requests.some(([path, method, body]) =>
    path === '/api/tasks/first/delete' && method === 'POST' && body.confirmation === 'first'));
  assert.equal(page.get('#page-title').textContent, 'second');
  assert.equal(page.get('#thread').children[0].textContent, 'second');
});

test('deleting a task from a chat keeps the chat and removes its obsolete task link', async () => {
  const page = await workbench();
  await page.get('#chats').children[0].click();
  assert.ok(page.get('#thread').children.some(node => node.tagName === 'button'));
  await page.remove(1);
  assert.equal(page.get('#thread').hidden, false);
  assert.equal(page.get('#thread').children[0].textContent, 'A separate chat');
  assert.equal(page.get('#thread').children.length, 1);
  assert.equal(page.get('#chats').children.length, 1);
});

test('a protected task stays selected and displays the server message', async () => {
  const page = await workbench({protectedId: 'first'});
  await page.open(0);
  await page.remove(0);
  assert.equal(page.get('#page-title').textContent, 'first');
  assert.equal(page.get('#tasks').children.length, 2);
  assert.match(page.get('#toast').textContent, /停止 \/ 取消运行并对账/);
});

test('stale list and detail responses cannot resurrect a deleted task', async () => {
  const page = await workbench();
  const releaseTask = page.hold('/api/tasks/first');
  const opening = page.open(0);
  const releaseList = page.hold('/api/state');
  const polling = page.poll();
  await page.remove(0);
  releaseTask();
  releaseList();
  await Promise.all([opening, polling]);
  assert.equal(page.get('#tasks').children.length, 1);
  assert.equal(page.get('#thread').hidden, true);
  assert.equal(page.get('#welcome').hidden, false);
});

test('splitters apply sizes with pointer and keyboard, clamp on resize, and reset', async () => {
  const page = await workbench();
  const sidebar = page.get('#sidebar-split');
  const history = page.get('#history-split');
  const reading = page.get('#reading-split');
  const px = element => Number(element.getAttribute('aria-valuenow'));
  for (const handle of [sidebar, history, reading]) {
    assert.equal(handle.getAttribute('role'), 'separator');
    assert.equal(handle.getAttribute('tabindex'), '0');
    assert.ok(handle.getAttribute('aria-label'));
  }
  assert.match(css, /width:var\(--sidebar-width,/);
  assert.match(css, /grid-template-rows:.*var\(--task-list-share,.*var\(--chat-list-share,/);
  assert.match(css, /height:var\(--draft-height,/);
  assert.match(css, /#sidebar-split\{display:none\}/);
  assert.deepEqual([px(sidebar), px(history), px(reading)], [258, 50, 62]);
  page.get('#menu').click();
  assert.ok(page.get('#sidebar').classList.contains('open'));
  page.get('#menu').click();
  assert.ok(!page.get('#sidebar').classList.contains('open'));

  sidebar.key('ArrowRight');
  assert.equal(page.shell.style.get('--sidebar-width'), '274px');
  sidebar.key('End');
  assert.equal(page.shell.style.get('--sidebar-width'), '400px');
  page.shell.clientWidth = 760;
  page.onResize();
  assert.equal(page.shell.style.get('--sidebar-width'), '280px');
  sidebar.key('Home');
  assert.equal(page.shell.style.get('--sidebar-width'), '210px');

  history.listeners.pointerdown({button: 0, pointerId: 1, clientY: 200});
  assert.equal(history.captured, 1);
  assert.ok(history.focused);
  history.listeners.pointermove({pointerId: 1, clientY: 240});
  assert.equal(page.shell.style.get('--task-list-share'), '60fr');
  assert.equal(page.shell.style.get('--chat-list-share'), '40fr');
  history.listeners.pointermove({pointerId: 1, clientY: 600});
  assert.equal(px(history), 75);
  history.listeners.lostpointercapture({pointerId: 1});
  history.listeners.pointermove({pointerId: 1, clientY: 200});
  assert.equal(px(history), 75);
  history.key('ArrowUp');
  assert.equal(px(history), 70);
  history.key('Home');
  assert.equal(px(history), 25);

  const readingHeight = () => page.conversation.clientHeight - page.composer.scrollHeight - reading.offsetHeight;
  const initialHeight = readingHeight();
  reading.key('ArrowUp');
  assert.equal(page.get('#prompt').offsetHeight, 78);
  assert.equal(readingHeight(), initialHeight - 16);
  reading.key('End');
  assert.equal(page.get('#prompt').offsetHeight, 240);
  page.conversation.clientHeight = 420;
  page.onResize();
  assert.equal(readingHeight(), 96);
  page.setControlsHeight(220);
  page.onResize();
  assert.equal(page.get('#prompt').offsetHeight, 90);
  assert.equal(readingHeight(), 96);

  page.shell.clientWidth = 1440;
  page.get('#reset-layout').click();
  assert.deepEqual([px(sidebar), px(history), px(reading)], [258, 50, 62]);
});
