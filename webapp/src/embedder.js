// embedder.js — 批量嵌入循环(书签文本 → 向量回写本地库)
export const QUERY_PREFIX = 'task: search result | query: ';
export const DOC_PREFIX = 'title: none | text: ';

export function buildEmbedText(handle, text) {
  return '@' + (handle || 'unknown') + ' ' + (text || '');
}

/**
 * 逐批嵌入所有缺向量的书签;onProgress(总处理数)。可随时中断(抛出即停)。
 */
export async function embedPending(store, engine, { batchSize = 16, onProgress } = {}) {
  let total = 0;
  for (;;) {
    const rows = await store.call('pending', { limit: batchSize });
    if (!rows.length) break;
    const resps = await engine.computeEmbeddingBatch(
      rows.map((r) => DOC_PREFIX + (r.embed_text || '')), { normalize: true });
    const vectors = rows.map((r, i) => {
      const f32 = Float32Array.from(resps[i].embedding);
      // sqlite-wasm 的 bind 只接受 Uint8Array 作为 BLOB
      return { id: r.tweet_id, vec: new Uint8Array(f32.buffer, f32.byteOffset, f32.byteLength), model: 'embeddinggemma' };
    });
    await store.call('storeVecs', { vectors });
    total += rows.length;
    if (onProgress) onProgress(total);
  }
  return total;
}
