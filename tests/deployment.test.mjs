import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');

async function fixture(t) {
  const target = await mkdtemp(join(tmpdir(), 'ocr-deployment-'));
  t.after(() => rm(target, { recursive: true, force: true }));
  const manifest = JSON.parse(await readFile(join(root, 'vendor-manifest.json'), 'utf8'));
  // Explicit source allowlist: never copy environment files or credentials.
  const files = ['package.json', 'vercel.json', 'vendor-manifest.json', 'site/index.html', ...Object.keys(manifest.sha256)];
  for (const folder of ['api', 'lib', 'scripts']) {
    for (const name of await readdir(join(root, folder))) {
      if (name.endsWith('.mjs')) files.push(`${folder}/${name}`);
    }
  }
  for (const path of files) {
    await mkdir(dirname(join(target, path)), { recursive: true });
    await copyFile(join(root, path), join(target, path));
  }
  return target;
}

function build(target, invocation = target) {
  const env = { ...process.env, VERCEL: '1', INIT_CWD: invocation };
  return spawnSync(process.execPath, ['scripts/build-site.mjs'], {
    cwd: target, env, encoding: 'utf8', timeout: 30000,
  });
}

test('clean Vercel build generates only the landing page in the configured output', async t => {
  const target = await fixture(t);
  const config = JSON.parse(await readFile(join(target, 'vercel.json'), 'utf8'));
  assert.equal(config.outputDirectory, 'public');
  assert.equal(config.installCommand, 'npm ci');
  const result = build(target);
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  assert.deepEqual(await readdir(join(target, config.outputDirectory)), ['index.html']);
  assert.equal(await readFile(join(target, 'public/index.html'), 'utf8'), await readFile(join(target, 'site/index.html'), 'utf8'));
});

test('wrong sandbox root fails before generating output, even after npm resets cwd', async t => {
  const target = await fixture(t);
  const result = build(target, join(target, 'sandbox'));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /OCR_DEPLOYMENT_ROOT_INVALID/);
  assert.match(result.stderr, /leave the field empty/);
  await assert.rejects(readdir(join(target, 'public')), { code: 'ENOENT' });
});

test('repeated builds replace stale generated files and preserve the source page', async t => {
  const target = await fixture(t);
  const original = await readFile(join(target, 'site/index.html'), 'utf8');
  assert.equal(build(target).status, 0);
  await writeFile(join(target, 'public/stale.txt'), 'obsolete build file');
  const next = `${original}\n<!-- updated source fixture -->\n`;
  await writeFile(join(target, 'site/index.html'), next);
  const result = build(target);
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  assert.deepEqual(await readdir(join(target, 'public')), ['index.html']);
  assert.equal(await readFile(join(target, 'public/index.html'), 'utf8'), next);
  assert.equal(await readFile(join(target, 'site/index.html'), 'utf8'), next);
});
