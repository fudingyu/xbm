// model.js — .litertlm 加载(WebGPU/浏览器内,与官方 embedding_search 同款管线)
import { Backend, EmbeddingEngine, getOrLoadGlobalLiteRtLm } from '@litert-lm/core';

function trackProgress(stream, total, cb) {
  if (!stream) return stream;
  let loaded = 0;
  return new ReadableStream({
    async start(controller) {
      const reader = stream.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        loaded += value.byteLength;
        cb(loaded, total);
        controller.enqueue(value);
      }
      controller.close();
    },
  });
}

/**
 * @param {File|string} source 本地 File 对象,或模型 URL(如 HF CDN / 本机后端 /model)
 * @param {(msg: string) => void} onStatus
 */
export async function loadEngine(source, onStatus) {
  onStatus('Loading embedding runtime (wasm)…');
  await getOrLoadGlobalLiteRtLm('./wasm');
  onStatus('Loading model (WebGPU)…');
  let stream;
  if (typeof source === 'string') {
    const resp = await fetch(source);
    if (!resp.ok) throw new Error(`Model URL HTTP ${resp.status}`);
    const total = +(resp.headers.get('Content-Length') || 0);
    stream = trackProgress(resp.body, total, (n, t) =>
      onStatus(`Model ${t ? Math.round((n / t) * 100) + '%' : Math.round(n / 1048576) + 'MB'}`));
  } else {
    stream = source.stream(); // File
  }
  const t0 = performance.now();
  // 纯文本 270m 模型不能传 vision/audio backend(会去找视觉编码器缓冲区)
  const engine = await EmbeddingEngine.create({ model: stream, backend: Backend.GPU });
  onStatus(`Model ready (loaded in ${((performance.now() - t0) / 1000).toFixed(1)}s)`);
  return engine;
}
