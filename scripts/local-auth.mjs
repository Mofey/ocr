import { loadEnvFile } from 'node:process';
import { fileURLToPath } from 'node:url';

// CLI setup only. Never generate credentials, infer another project, print
// secrets or create a sandbox while the intended project's auth is missing.
export function loadLocalAuthentication() {
  const path = fileURLToPath(new URL('../.env.local', import.meta.url));
  try {
    loadEnvFile(path);
  } catch (error) {
    if (!(error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT')) {
      console.error('Could not read the OCR project\'s .env.local file. Check its format and file permissions.');
      return false;
    }
  }
  if (process.env.VERCEL_TOKEN) {
    if ([process.env.VERCEL_TOKEN, process.env.VERCEL_TEAM_ID, process.env.VERCEL_PROJECT_ID].every(value => value?.trim())) return true;
    console.error('Access-token authentication requires VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID together.');
    return false;
  }
  if (process.env.VERCEL_OIDC_TOKEN?.trim()) return true;
  console.error([
    'Vercel authentication is missing. From your OCR project folder, run:',
    '  npx vercel login',
    '  npx vercel link',
    '  npx vercel env pull .env.local',
    'Select your existing OCR worker project when linking, then retry the npm command.',
    'env pull creates .env.local with the Vercel authentication token.',
    'Copying .env.example or creating an empty file does not provide authentication.',
  ].join('\n'));
  return false;
}
