import { copyFile, mkdir, realpath, rm, stat } from 'node:fs/promises';
import { dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
// npm runs scripts from the directory containing package.json even when Vercel
// invokes it in a child folder. INIT_CWD retains that original invocation path.
if (process.env.VERCEL === '1' || process.env.__VERCEL_BUILD_RUNNING === '1') {
  const invocationRoot = await realpath(process.env.INIT_CWD || process.cwd());
  if (invocationRoot !== await realpath(root)) {
    throw new Error([
      'OCR_DEPLOYMENT_ROOT_INVALID: Vercel is building a different directory from the OCR gateway.',
      'In Vercel Settings > Build and Deployment > Root Directory, clear "sandbox" and leave the field empty for the standalone OCR repository.',
      'For the Ledgerical monorepo only, set Root Directory to "ocr". Save and redeploy.',
    ].join('\n'));
  }
}

await import('./check.mjs');

const output = resolve(root, 'public');
if (output === root || !output.startsWith(`${root}${sep}`)) {
  throw new Error('Refusing to write the static build outside the project directory.');
}

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await copyFile(resolve(root, 'site/index.html'), resolve(output, 'index.html'));
const page = await stat(resolve(output, 'index.html'));
if (!page.isFile() || page.size === 0) throw new Error('The generated public/index.html is missing or empty.');
console.log(`Static landing page written to public/index.html (${page.size} bytes).`);
