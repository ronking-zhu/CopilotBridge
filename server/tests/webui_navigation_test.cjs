const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '..', 'webapp', 'v23.js'), 'utf8');

function sourceBetween(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0 && end > start, 'The real callback block must exist');
  return source.slice(start, end);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function navigationFixture(view = 'sessions', preserveInitialView = false) {
  const operation = deferred();
  const state = { view, navigationRevision: 0 };
  const context = vm.createContext({
    v23State: state,
    v23PreserveInitialView: preserveInitialView,
    openSession: () => operation.promise,
    newSession: () => operation.promise,
    v28RefreshGenerateButtons: () => {},
    v23ShowView: nextView => { state.view = nextView; state.navigationRevision += 1; },
  });
  vm.runInContext(sourceBetween(
    '  const v23LegacyOpenSession = openSession;',
    '  openInboxItem = v23SelectInbox;',
  ), context);
  return { context, operation, state };
}

for (const action of ['openSession', 'newSession']) {
  test(`${action}: a late response preserves the user's knowledge navigation`, async () => {
    const { context, operation, state } = navigationFixture();
    const pending = context[action]('fixture-session');
    context.v23ShowView('knowledge');
    operation.resolve('loaded');
    assert.equal(await pending, 'loaded');
    assert.equal(state.view, 'knowledge');
  });

  test(`${action}: normal completion opens the conversation`, async () => {
    const { context, operation, state } = navigationFixture();
    const pending = context[action]('fixture-session');
    operation.resolve();
    await pending;
    assert.equal(state.view, 'conversation');
  });

  for (const initialView of ['inbox', 'knowledge']) {
    test(`${action}: initial ${initialView} links remain selected`, async () => {
      const { context, operation, state } = navigationFixture(initialView, true);
      const pending = context[action]('fixture-session');
      operation.resolve();
      await pending;
      assert.equal(state.view, initialView);
      assert.equal(context.v23PreserveInitialView, false);
      await context[action]('fixture-session');
      assert.equal(state.view, 'conversation');
    });
  }
}

test('dashboard disclosure updates visibility and accessible state together', () => {
  const attributes = {};
  const controls = { hidden: false };
  const toggle = { setAttribute: (name, value) => { attributes[name] = value; } };
  const context = vm.createContext({
    v29DashboardControls: controls,
    v29DashboardToggle: toggle,
    v23T: (_zh, en) => en,
  });
  vm.runInContext(sourceBetween(
    '  function v29SetDashboardExpanded(',
    '  let v29DashboardExpanded = false;',
  ), context);
  for (const expanded of [false, true, false]) {
    context.v29SetDashboardExpanded(expanded);
    assert.equal(controls.hidden, !expanded);
    assert.equal(attributes['aria-expanded'], String(expanded));
    assert.equal(attributes['aria-label'], toggle.title);
    assert.match(toggle.title, expanded ? /^Hide / : /^Show /);
  }
});

function generationFixture() {
  const submission = deferred();
  const calls = [];
  const alerts = [];
  const pollDelays = [];
  const state = { knowledgeJobs: [], knowledgeSubmissions: new Set(), knowledgeJobsRequest: 0, view: 'knowledge' };
  const button = { setAttribute: () => {}, removeAttribute: () => {} };
  const context = vm.createContext({
    v23State: state,
    v23Icons: { knowledgeGenerate: '<svg></svg>' },
    v23T: (_zh, en) => en,
    sessions: [{ id: 'fixture', title: 'Synthetic conversation' }],
    sid: 'fixture',
    dashboardInitialized: true,
    document: {
      querySelector: selector => selector === '#v26MapScope' ? { value: '48' } : button,
      querySelectorAll: () => [],
    },
    api: (method, path) => {
      calls.push({ method, path });
      return method === 'POST' ? submission.promise : Promise.resolve({ jobs: [] });
    },
    alert: message => alerts.push(message),
    v28RenderKnowledgeJobs: () => {},
    v28ScheduleKnowledgeJobs: delay => pollDelays.push(delay),
    v26SetKnowledgeMode: () => {},
    v23ShowView: () => {},
  });
  vm.runInContext(sourceBetween('  function v28KnowledgeJob(', '  function v28RenderKnowledgeJobs('), context);
  vm.runInContext(sourceBetween('  async function v28LoadKnowledgeJobs(', '  function v26SetKnowledgeMode('), context);
  vm.runInContext(sourceBetween('  async function v27GenerateConversationKnowledge(', '  function v23SettingSection('), context);
  return { context, submission, calls, alerts, pollDelays, state, button };
}

test('polling cannot hide a pending submission or allow duplicate generation', async () => {
  const { context, submission, calls, state, button } = generationFixture();
  const pending = context.v27GenerateConversationKnowledge('fixture', button);
  assert.equal(button.disabled, true);
  await context.v28LoadKnowledgeJobs();
  assert.equal(state.knowledgeJobs[0].status, 'queued');
  assert.equal(button.disabled, true);
  await context.v27GenerateConversationKnowledge('fixture', button);
  assert.equal(calls.filter(call => call.method === 'POST').length, 1);
  submission.resolve({ job: { conversationId: 'fixture', status: 'running' } });
  await pending;
  assert.equal(state.knowledgeSubmissions.size, 0);
  assert.equal(state.knowledgeJobs[0].status, 'running');
  assert.equal(button.disabled, true);
});

test('failed or invalid submissions show an error and unlock retry', async () => {
  for (const response of [new Error('Synthetic request failure'), null, {}]) {
    const { context, submission, alerts, pollDelays, state, button } = generationFixture();
    const pending = context.v27GenerateConversationKnowledge('fixture', button);
    if (response instanceof Error) submission.reject(response);
    else submission.resolve(response);
    await pending;
    assert.equal(alerts.length, 1);
    assert.match(alerts[0], /Could not start knowledge generation/);
    assert.equal(state.knowledgeSubmissions.size, 0);
    assert.equal(state.knowledgeJobs.length, 0);
    assert.equal(button.disabled, false);
    assert.equal(pollDelays.at(-1), 250);
  }
});

test('a poll started before submission cannot erase the acknowledged job', async () => {
  const { context, submission, state, button } = generationFixture();
  const listing = deferred();
  context.api = method => method === 'GET' ? listing.promise : submission.promise;
  const poll = context.v28LoadKnowledgeJobs();
  const pending = context.v27GenerateConversationKnowledge('fixture', button);
  submission.resolve({ job: { conversationId: 'fixture', status: 'running' } });
  await pending;
  listing.resolve({ jobs: [] });
  await poll;
  assert.equal(state.knowledgeJobs[0].status, 'running');
  assert.equal(button.disabled, true);
});