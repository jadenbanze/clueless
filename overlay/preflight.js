'use strict';

// System requirements check, run before the API starts.
//
// Split in two so the decision logic is testable without a Mac in a
// particular state: gatherFacts() probes the machine, evaluate() turns facts
// into blocking failures and warnings. Plain Node — no Electron APIs — so
// `npm run preflight` works headless in CI.

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFile } = require('child_process');

const MIN_DARWIN_MAJOR = 25; // macOS 26 — SpeechTranscriber and Foundation Models
const MIN_FREE_GB_BLOCKING = 2;
const MIN_FREE_GB_WARNING = 10;
const RECOMMENDED_RAM_GB = 16;

// Local model servers worth probing. Loopback only: preflight must never make
// an outbound connection.
const PROVIDER_PROBES = [
  { id: 'ollama', label: 'Ollama', url: 'http://127.0.0.1:11434/api/tags' },
  { id: 'llamacpp', label: 'llama.cpp server', url: 'http://127.0.0.1:8080/v1/models' },
  { id: 'llamacpp-alt', label: 'llama.cpp server', url: 'http://127.0.0.1:8081/v1/models' },
  { id: 'lmstudio', label: 'LM Studio', url: 'http://127.0.0.1:1234/v1/models' },
];

function run(file, args, timeoutMs = 8000) {
  return new Promise((resolve) => {
    execFile(file, args, { timeout: timeoutMs }, (error, stdout) => {
      resolve({ code: error ? (typeof error.code === 'number' ? error.code : -1) : 0, stdout: String(stdout ?? '') });
    });
  });
}

function parseJsonLine(text) {
  const line = text.trim().split('\n').pop();
  try { return JSON.parse(line); } catch { return null; }
}

async function probeUrl(url, timeoutMs = 800) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Probes this machine. Every probe tolerates failure: a missing helper or a
 * hung sysctl becomes "unknown", which evaluate() treats as a warning at most.
 */
async function gatherFacts({ dataDir, projectRoot, locale = 'en-US' } = {}) {
  const facts = {
    platform: process.platform,
    darwinMajor: Number.parseInt(os.release().split('.')[0], 10),
    macosVersion: null,
    processArch: process.arch,
    arm64Hardware: null,
    totalMemGB: os.totalmem() / 1024 ** 3,
    freeDiskGB: null,
    providers: [],
    foundation: null,
    speech: null,
  };

  if (process.platform === 'darwin') {
    const version = await run('/usr/bin/sw_vers', ['-productVersion']);
    facts.macosVersion = version.stdout.trim() || null;
    // process.arch reports x64 under Rosetta; the sysctl reports the hardware.
    const arm = await run('/usr/sbin/sysctl', ['-n', 'hw.optional.arm64']);
    facts.arm64Hardware = arm.code === 0 ? arm.stdout.trim() === '1' : false;
  }

  try {
    const target = dataDir && fs.existsSync(dataDir) ? dataDir : os.homedir();
    const stats = fs.statfsSync(target);
    facts.freeDiskGB = (stats.bavail * stats.bsize) / 1024 ** 3;
  } catch { /* unknown */ }

  const results = await Promise.all(PROVIDER_PROBES.map(async (probe) => ({
    ...probe, running: await probeUrl(probe.url),
  })));
  facts.providers = results.filter((probe) => probe.running).map(({ id, label }) => ({ id, label }));

  const build = projectRoot ? path.join(projectRoot, 'native', 'build') : null;
  if (build) {
    const fmcli = path.join(build, 'fmcli');
    if (fs.existsSync(fmcli)) {
      const result = await run(fmcli, ['--check'], 5000);
      facts.foundation = parseJsonLine(result.stdout)?.available === true;
    }
    const audiotap = path.join(build, 'CluelessTap.app', 'Contents', 'MacOS', 'audiotap');
    if (fs.existsSync(audiotap)) {
      const result = await run(audiotap, ['--speech-check', '--locale', locale], 10000);
      facts.speech = parseJsonLine(result.stdout);
    }
  }
  return facts;
}

/** Pure: facts in, verdict out. */
function evaluate(facts) {
  const blocking = [];
  const warnings = [];
  const passed = [];

  if (facts.platform !== 'darwin' || !(facts.darwinMajor >= MIN_DARWIN_MAJOR)) {
    blocking.push({
      id: 'macos',
      title: 'macOS 26 or later is required',
      detail: `This Mac is running ${facts.macosVersion ? `macOS ${facts.macosVersion}` : facts.platform}. `
        + 'On-device transcription uses the Speech framework introduced in macOS 26.',
    });
  } else {
    passed.push({ id: 'macos', title: `macOS ${facts.macosVersion ?? facts.darwinMajor + 1}` });
  }

  if (facts.platform === 'darwin' && facts.arm64Hardware === false) {
    blocking.push({
      id: 'arch',
      title: 'Apple silicon is required',
      detail: 'Clueless is built for M-series Macs. Intel Macs are not supported.',
    });
  } else if (facts.platform === 'darwin') {
    passed.push({ id: 'arch', title: 'Apple silicon' });
  }

  if (facts.freeDiskGB !== null && facts.freeDiskGB < MIN_FREE_GB_BLOCKING) {
    blocking.push({
      id: 'disk',
      title: 'Not enough free disk space',
      detail: `${facts.freeDiskGB.toFixed(1)} GB free; at least ${MIN_FREE_GB_BLOCKING} GB is needed for the search index and logs.`,
    });
  } else if (facts.freeDiskGB !== null && facts.freeDiskGB < MIN_FREE_GB_WARNING) {
    warnings.push({
      id: 'disk',
      title: 'Low disk space',
      detail: `${facts.freeDiskGB.toFixed(1)} GB free. A local model needs 2–5 GB; consider freeing space before downloading one.`,
    });
  } else if (facts.freeDiskGB !== null) {
    passed.push({ id: 'disk', title: `${Math.round(facts.freeDiskGB)} GB free` });
  }

  if (facts.totalMemGB < RECOMMENDED_RAM_GB) {
    warnings.push({
      id: 'ram',
      title: `${Math.round(facts.totalMemGB)} GB memory`,
      detail: 'An 8B model may be slow. A 3B model such as llama3.2 is recommended on this Mac.',
      recommendModel: 'llama3.2',
    });
  } else {
    passed.push({ id: 'ram', title: `${Math.round(facts.totalMemGB)} GB memory` });
  }

  if (facts.providers.length === 0 && facts.foundation !== true) {
    warnings.push({
      id: 'provider',
      title: 'No local model found',
      detail: 'Install Ollama, run a llama.cpp server, or choose a GGUF file in Settings → Model. '
        + 'Transcription works without one; answers and summaries do not.',
    });
  } else {
    const names = facts.providers.map((provider) => provider.label);
    if (facts.foundation) names.push('Apple Intelligence');
    passed.push({ id: 'provider', title: `Model: ${[...new Set(names)].join(', ')}` });
  }

  if (facts.speech && facts.speech.supported === false) {
    warnings.push({
      id: 'speech',
      title: `Speech locale ${facts.speech.locale} is not supported`,
      detail: 'Choose a different locale in Settings → General.',
    });
  } else if (facts.speech && facts.speech.installed === false) {
    warnings.push({
      id: 'speech',
      title: 'Speech model not downloaded yet',
      detail: `macOS will download the ${facts.speech.locale} model from Apple the first time you start a meeting.`,
    });
  } else if (facts.speech) {
    passed.push({ id: 'speech', title: `Speech model ${facts.speech.locale}` });
  }

  return { ok: blocking.length === 0, blocking, warnings, passed };
}

/** Plain-text report for "Copy report" and the CLI. */
function formatReport(facts, verdict) {
  const lines = [`Clueless preflight — ${verdict.ok ? 'OK' : 'BLOCKED'}`];
  for (const item of verdict.blocking) lines.push(`✗ ${item.title}: ${item.detail}`);
  for (const item of verdict.warnings) lines.push(`! ${item.title}: ${item.detail}`);
  for (const item of verdict.passed) lines.push(`✓ ${item.title}`);
  lines.push('', `macOS ${facts.macosVersion ?? '?'} (Darwin ${facts.darwinMajor}), `
    + `arch ${facts.processArch}${facts.arm64Hardware ? ' / arm64 hardware' : ''}, `
    + `${facts.totalMemGB.toFixed(0)} GB RAM, `
    + `${facts.freeDiskGB === null ? '?' : facts.freeDiskGB.toFixed(1)} GB free`);
  return lines.join('\n');
}

async function runPreflight(options) {
  const facts = await gatherFacts(options);
  const verdict = evaluate(facts);
  return { facts, verdict, report: formatReport(facts, verdict) };
}

module.exports = { gatherFacts, evaluate, formatReport, runPreflight, MIN_DARWIN_MAJOR };

if (require.main === module) {
  const json = process.argv.includes('--json');
  runPreflight({ projectRoot: path.join(__dirname, '..') }).then(({ facts, verdict, report }) => {
    console.log(json ? JSON.stringify({ facts, verdict }, null, 2) : report);
    process.exit(verdict.ok ? 0 : 1);
  });
}
