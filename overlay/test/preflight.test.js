'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { evaluate, formatReport } = require('../preflight');

const healthy = {
  platform: 'darwin',
  darwinMajor: 25,
  macosVersion: '26.1',
  processArch: 'arm64',
  arm64Hardware: true,
  totalMemGB: 36,
  freeDiskGB: 200,
  providers: [{ id: 'ollama', label: 'Ollama' }],
  foundation: true,
  speech: { locale: 'en_US', supported: true, installed: true },
};

const ids = (items) => items.map((item) => item.id);

test('a supported Mac passes with no warnings', () => {
  const verdict = evaluate(healthy);
  assert.equal(verdict.ok, true);
  assert.deepEqual(verdict.blocking, []);
  assert.deepEqual(verdict.warnings, []);
});

test('macOS 15 is blocked', () => {
  const verdict = evaluate({ ...healthy, darwinMajor: 24, macosVersion: '15.5' });
  assert.equal(verdict.ok, false);
  assert.deepEqual(ids(verdict.blocking), ['macos']);
  assert.match(verdict.blocking[0].detail, /15\.5/);
});

test('non-macOS is blocked', () => {
  assert.equal(evaluate({ ...healthy, platform: 'linux' }).ok, false);
});

test('Intel hardware is blocked even if process.arch looks fine', () => {
  const verdict = evaluate({ ...healthy, arm64Hardware: false });
  assert.deepEqual(ids(verdict.blocking), ['arch']);
});

test('under 2 GB free blocks; under 10 GB warns', () => {
  assert.deepEqual(ids(evaluate({ ...healthy, freeDiskGB: 1.2 }).blocking), ['disk']);
  const low = evaluate({ ...healthy, freeDiskGB: 6 });
  assert.equal(low.ok, true);
  assert.deepEqual(ids(low.warnings), ['disk']);
});

test('unknown disk space is neither blocking nor a warning', () => {
  const verdict = evaluate({ ...healthy, freeDiskGB: null });
  assert.equal(verdict.ok, true);
  assert.deepEqual(verdict.warnings, []);
});

test('8 GB RAM recommends a 3B model', () => {
  const verdict = evaluate({ ...healthy, totalMemGB: 8 });
  assert.equal(verdict.ok, true);
  assert.equal(verdict.warnings[0].id, 'ram');
  assert.equal(verdict.warnings[0].recommendModel, 'llama3.2');
});

test('no provider warns but does not block', () => {
  const verdict = evaluate({ ...healthy, providers: [], foundation: false });
  assert.equal(verdict.ok, true);
  assert.deepEqual(ids(verdict.warnings), ['provider']);
});

test('Apple Intelligence alone counts as a provider', () => {
  assert.deepEqual(evaluate({ ...healthy, providers: [], foundation: true }).warnings, []);
});

test('missing speech assets warn; unsupported locale warns differently', () => {
  const missing = evaluate({ ...healthy, speech: { locale: 'en_GB', supported: true, installed: false } });
  assert.match(missing.warnings[0].title, /not downloaded/);
  const unsupported = evaluate({ ...healthy, speech: { locale: 'xx-ZZ', supported: false, installed: false } });
  assert.match(unsupported.warnings[0].title, /not supported/);
});

test('report names every blocking failure', () => {
  const facts = { ...healthy, darwinMajor: 24, arm64Hardware: false };
  const report = formatReport(facts, evaluate(facts));
  assert.match(report, /BLOCKED/);
  assert.match(report, /macOS 26 or later/);
  assert.match(report, /Apple silicon/);
});
