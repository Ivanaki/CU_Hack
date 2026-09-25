const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = readFileSync(path.join(__dirname, '..', 'miniapp', 'dist', 'index.html'), 'utf8');
const start = html.indexOf('function fitViewport() {');
const end = html.indexOf('\nfitViewport();', start);
assert.ok(start >= 0 && end > start, 'Mini App viewport handler exists');
const handler = html.slice(start, end);

function measure({ inner, stable, current }) {
  const properties = new Map();
  const root = { setProperty: (name, value) => properties.set(name, value) };
  const tg = { viewportStableHeight: stable, viewportHeight: current };
  const document = {
    documentElement: { style: root },
    getElementById: () => ({ getBoundingClientRect: () => ({ height: 68 }) }),
  };
  vm.runInNewContext(`${handler}\nfitViewport();`, {
    tg, document, window: { innerHeight: inner }, Math,
  });
  return Object.fromEntries(properties);
}

test('navigation stays at the bottom when Mini App reopens before stable height catches up', () => {
  const layout = measure({ inner: 800, stable: 450, current: 800 });
  assert.equal(layout['--eaten'], '0px');
  assert.equal(layout['--vh'], '800px');
});

test('navigation still clears the genuinely hidden bottom strip', () => {
  const layout = measure({ inner: 800, stable: 740, current: 740 });
  assert.equal(layout['--eaten'], '60px');
});
