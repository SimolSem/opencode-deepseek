// After an emit the controller compares the rendered answer bubble with the
// emitted text. Synthetic DOM snapshots only; metadata is the only output.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../browser/opencode-controller.user.js'), 'utf8');
const start = source.indexOf('  // ---- DOM check');
const end = source.indexOf('\n  button.addEventListener("click"');
assert.ok(start > 0 && end > start, 'domCheck not found');
const fnSource = source.slice(start, end);

async function runCheck({ wire, bubbles, stopAt = null }) {
  let clock = 0;
  const context = vm.createContext({
    console: { warn() {} },
    performance: { now: () => clock },
    setTimeout: (fn, ms) => { clock += ms || 0; queueMicrotask(fn); },
    snapshot: () => ['older answer', bubbles(clock)],
  });
  const script = `let domCheckToken = null, pendingDomCheck = null;\n${fnSource}\n`
    + `const stopAt = ${JSON.stringify(stopAt)};\n`
    + `const run = domCheck(${JSON.stringify(wire)}, snapshot);\n`
    + `(async () => {\n`
    + `  if (stopAt !== null) { while (performance.now() < stopAt) await new Promise(r => setTimeout(r, 50)); domCheckToken = null; }\n`
    + `  return { result: await run, pending: pendingDomCheck };\n`
    + `})()`;
  return vm.runInContext(script, context);
}

(async () => {
  // Markdown rendering differences still match (letters and digits only).
  let { result, pending } = await runCheck({
    wire: 'Done: **read** `src/app.py`\n\n- one\n- two',
    bubbles: () => 'Done: read src/app.py\n\none\ntwo',
    stopAt: 1000,
  });
  assert.equal(result.match, true);
  assert.equal(result.similarity, 100);
  assert.equal(result.ended, 'next_job');
  assert.equal(result.changes, 0);
  assert.deepEqual(pending, result);

  // The bubble is rewritten after emit into a different answer.
  ({ result } = await runCheck({
    wire: '```tool_calls\n[{"name":"grep","arguments":{"pattern":"foo"}}]\n```',
    bubbles: clock => (clock < 2000
      ? '```tool_calls\n[{"name":"grep","arguments":{"pattern":"foo"}}]\n```'
      : 'I found nothing relevant in the project files.'),
    stopAt: 4000,
  }));
  assert.equal(result.match, false);
  assert.ok(result.similarity < 30, 'a different answer has low similarity');
  assert.equal(result.changes, 1);
  assert.equal(result.tool_wire, true);
  assert.equal(result.tool_dom, false);

  // Markdown round-trip noise (a link URL lost in rendering) is still the same answer.
  const prose = Array.from({ length: 100 }, (_, i) => 'word' + i).join(' ');
  ({ result } = await runCheck({
    wire: prose + ' see [docs](https://example.com/guide)',
    bubbles: () => prose + ' see docs',
    stopAt: 500,
  }));
  assert.equal(result.exact, false);
  assert.ok(result.similarity >= 97 && result.similarity < 100);
  assert.equal(result.match, true);

  // A rendered diagram adds DOM text: low similarity, but every emitted word is there.
  ({ result } = await runCheck({
    wire: prose,
    bubbles: () => prose + ' ' + Array.from({ length: 150 }, (_, i) => 'node' + i).join(' '),
    stopAt: 500,
  }));
  assert.equal(result.match, false);
  assert.ok(result.similarity < 70);
  assert.equal(result.coverage, 100);

  // Without a next job the watch ends on its own and keeps the last state.
  ({ result } = await runCheck({ wire: 'same', bubbles: () => 'same' }));
  assert.equal(result.ended, 'timeout');
  assert.equal(result.match, true);
  // Metadata only: no text field leaves the check.
  assert.deepEqual(Object.keys(result).sort(), [
    'changes', 'coverage', 'dom_len', 'ended', 'exact', 'found', 'match', 'similarity', 'stable_ms', 'tool_dom', 'tool_wire', 'wire_len',
  ]);
  console.log('PASS: DOM check compares rendered answer with emitted wire');
})().catch(error => { console.error(error); process.exitCode = 1; });
