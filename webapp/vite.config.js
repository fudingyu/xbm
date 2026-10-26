// 静态版构建:litert wasm 拷入 dist/wasm(与主前端同款),sqlite-wasm 由打包器处理
import { cpSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));

export default {
  base: './',
  worker: { format: 'es' },
  optimizeDeps: { exclude: ['@sqlite.org/sqlite-wasm'] },
  build: { emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 2000, target: 'es2022' },
  plugins: [
    {
      name: 'copy-litert-wasm',
      closeBundle() {
        const corePkg = require.resolve('@litert-lm/core/package.json');
        cpSync(resolve(corePkg, '..', 'wasm'), resolve(here, 'dist', 'wasm'), {
          recursive: true,
        });
        console.log('Copied @litert-lm/core/wasm -> dist/wasm');
      },
    },
  ],
};
