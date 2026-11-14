import { loadEnvFile } from 'node:process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function refreshLocalAuthentication(reason) {
  const npx = process.platform === 'win32' ? 'npx.cmd' : 'npx';
  console.error([
    reason,
    'From the OCR project folder, refresh your local Vercel login:',
    `  ${npx} vercel env pull .env.local`,
    'If asked, select the existing OCR worker project, then retry the npm command.',
    'If login or linking is required, run vercel login and vercel link first.',
  ].join('\n'));
  return false;
}

function validateLocalOidcToken(token) {
  try {
    // This is a local expiry/scope preflight, not signature verification or
    // authorization. Vercel still validates the credential on every API call.
    const parts = token.split('.');
    if (parts.length !== 3 || token.length > 32768 || !parts.every(part => /^[A-Za-z0-9_-]+$/.test(part))) throw new Error();
    const claims = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
    if (!claims || !Number.isSafeInteger(claims.exp)
        || typeof claims.owner_id !== 'string' || !claims.owner_id.trim()
        || typeof claims.project_id !== 'string' || !claims.project_id.trim()) throw new Error();
    if (claims.exp * 1000 <= Date.now() + 300000) {
      return refreshLocalAuthentication('Local Vercel authentication has expired or expires within five minutes. No sandbox creation was attempted.');
    }
    return true;
  } catch {
    return refreshLocalAuthentication('Local Vercel authentication is invalid. No sandbox creation was attempted.');
  }
}

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
  // Match the SDK's file-first credential selection. Never print token values,
  // decoded claims, filesystem paths or raw parsing errors.
  if (process.env.VERCEL_OIDC_TOKEN_FILE?.trim()) {
    try { return validateLocalOidcToken(readFileSync(process.env.VERCEL_OIDC_TOKEN_FILE, 'utf8').trim()); }
    catch {
      console.error('Could not read VERCEL_OIDC_TOKEN_FILE. Check its location and permissions. No sandbox creation was attempted.');
      return false;
    }
  }
  if (process.env.VERCEL_OIDC_TOKEN?.trim()) return validateLocalOidcToken(process.env.VERCEL_OIDC_TOKEN.trim());
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
