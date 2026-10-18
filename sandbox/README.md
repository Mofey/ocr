# Parser image sources

This folder contains the Python parser and Docker image used by the isolated
OCR sandbox. Prepare that image from the repository root with
`npm run prepare:snapshot`.

Deploy the Node.js gateway from the repository root, which contains
`package.json`, `vercel.json` and `api/`. In the standalone repository's Vercel
settings, leave **Root Directory empty**. Selecting `sandbox` will make Vercel
build the wrong directory.
