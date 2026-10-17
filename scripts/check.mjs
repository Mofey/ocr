// Static build checks only. No worker, sandbox, inference or browser is run.
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
for (const folder of ['api', 'lib', 'scripts']) {
  for (const name of readdirSync(resolve(root, folder)).filter(name => name.endsWith('.mjs'))) {
    const result = spawnSync(process.execPath, ['--check', resolve(root, folder, name)], { stdio: 'inherit' });
    if (result.status !== 0) process.exit(result.status ?? 1);
  }
}
const manifest = JSON.parse(readFileSync(resolve(root, 'vendor-manifest.json'), 'utf8'));
for (const [path, expected] of Object.entries(manifest.sha256)) {
  const actual = createHash('sha256').update(readFileSync(resolve(root, path))).digest('hex');
  if (actual !== expected) throw new Error(`Worker source changed: ${path}; review and update the manifest.`);
}
JSON.parse(readFileSync(resolve(root, 'vercel.json'), 'utf8'));
console.log('JavaScript syntax, configuration JSON and worker source hashes are valid.');
