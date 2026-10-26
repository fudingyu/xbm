// search.js — 余弦 + 子串 → RRF 融合(与 Python search.py 同款打分)
export async function hybridSearch(store, engine, query, k = 50) {
  const resp = await engine.computeEmbedding(QUERY_PREFIX + query, { normalize: true });
  const qv = Float32Array.from(resp.embedding);
  const rows = await store.call('allVecs');
  const scored = [];
  const subHits = [];
  const qLower = query.toLowerCase();
  for (const r of rows) {
    if (!r.embedding || !r.embedding.length) continue;
    const v = new Float32Array(r.embedding.buffer, r.embedding.byteOffset,
      r.embedding.byteLength / 4);
    let sim = 0;
    for (let i = 0; i < v.length; i++) sim += v[i] * qv[i];
    scored.push({ row: r, sim });
    if (subHits.length < 400 && (r.text || '').toLowerCase().includes(qLower)) subHits.push(r);
  }
  scored.sort((a, b) => b.sim - a.sim);
  // RRF(与 Python 同参数 k=60)
  const score = new Map();
  const top = scored.slice(0, k);
  top.forEach((h, i) =>
    score.set(h.row.tweet_id, (score.get(h.row.tweet_id) || 0) + 1 / (60 + i + 1)));
  subHits.slice(0, k).forEach((h, i) =>
    score.set(h.tweet_id, (score.get(h.tweet_id) || 0) + 1 / (60 + i + 1)));
  const byId = new Map(scored.map((h) => [h.row.tweet_id, h]));
  const merged = [...score.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, k)
    .map(([tid, s]) => {
      const h = byId.get(tid) || { row: subHits.find((r) => r.tweet_id === tid), sim: null };
      let media = [], urls = [];
      try { media = JSON.parse(h.row.media_json || '[]'); } catch { /* ignore */ }
      try { urls = JSON.parse(h.row.urls_json || '[]'); } catch { /* ignore */ }
      return {
        tweet_id: tid, text: h.row.text, author_handle: h.row.author_handle,
        created_at: h.row.created_at, sim: h.sim,
        fts_hit: subHits.some((r) => r.tweet_id === tid),
        media, urls,
      };
    });
  return merged;
}
