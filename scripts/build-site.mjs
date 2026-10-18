import { copyFile, mkdir, readFile, rm } from 'node:fs/promises';
import { dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const config = JSON.parse(await readFile(resolve(root, 'vercel.json'), 'utf8'));
if (config.outputDirectory !== 'dist') {
  throw new Error('vercel.json must point outputDirectory to dist.');
}
const output = resolve(root, config.outputDirectory);
if (output === root || !output.startsWith(`${root}${sep}`)) {
  throw new Error('Refusing to write the static build outside the project directory.');
}

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await copyFile(resolve(root, 'site/index.html'), resolve(output, 'index.html'));
console.log('Static landing page written to dist/.');
