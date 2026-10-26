// 与官方 embedding_search 示例同款处理:构建时把 @litert-lm/core/wasm 拷进 dist/wasm
import { cpSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));

export default {
  base: './',
  build: { emptyOutDir: true, sourcemap: false },
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
