// store.js — db-worker 的主线程 RPC 客户端
export class Store {
  constructor() {
    this.seq = 0;
    this.pending = new Map();
    this.worker = new Worker(new URL('./db-worker.js', import.meta.url), { type: 'module' });
    this.worker.onmessage = (e) => {
      const { id, ok, result, error } = e.data || {};
      const p = this.pending.get(id);
      if (!p) return;
      this.pending.delete(id);
      ok ? p.resolve(result) : p.reject(new Error(error));
    };
    this.worker.onerror = (e) => {
      // worker 崩溃(如 sahpool 双开):把所有挂起调用打失败
      for (const [, p] of this.pending) p.reject(new Error(e.message || 'worker error'));
      this.pending.clear();
    };
  }

  call(op, payload = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ id, op, payload });
    });
  }
}
