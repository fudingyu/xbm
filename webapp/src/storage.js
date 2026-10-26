// storage.js — 数据库存储位置管理(静态版):
//  - opfs 模式:浏览器私有存储(默认,免授权,不可见文件)
//  - file 模式:File System Access API,用户选定的真实 .db 文件(可见、可放进
//    网盘同步目录如 Google Drive;句柄持久化在 IndexedDB,重开页面一键重连)
// 约束:showOpenFilePicker/showSaveFilePicker 仅 Chrome/Edge;Firefox/Safari 自动降级 OPFS。
import { Store } from './store.js';

const IDB_NAME = 'xbm-fs';
const IDB_KEY = 'db-file-handle';

function idb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbSet(key, value) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('kv', 'readwrite');
    tx.objectStore('kv').put(value, key);
    tx.oncomplete = () => resolve(true);
    tx.onerror = () => reject(tx.error);
  });
}
async function idbGet(key) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('kv', 'readonly');
    const req = tx.objectStore('kv').get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idbDel(key) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('kv', 'readwrite');
    tx.objectStore('kv').delete(key);
    tx.oncomplete = () => resolve(true);
    tx.onerror = () => reject(tx.error);
  });
}

export const pickerSupported = typeof window.showOpenFilePicker === 'function';

export async function initStorage(store) {
  const saved = await idbGet(IDB_KEY).catch(() => null);
  if (!saved) return { mode: 'opfs' };
  const perm = await saved.queryPermission({ mode: 'readwrite' });
  if (perm !== 'granted') {
    return { mode: 'reconnect-needed', handle: saved, name: saved.name };
  }
  return connectHandle(store, saved);
}

export async function connectHandle(store, handle) {
  const perm = await handle.requestPermission({ mode: 'readwrite' });
  if (perm !== 'granted') throw new Error('permission denied');
  const file = await handle.getFile();
  const bytes = new Uint8Array(await file.arrayBuffer());
  await idbSet(IDB_KEY, handle);
  const r = await store.call('setFileHandle', { handle, bytes, name: handle.name });
  return { mode: 'file', name: handle.name, size: bytes.byteLength, ...r };
}

export async function pickNewDb(store) {
  if (!pickerSupported) throw new Error('此浏览器不支持选择本地文件(需 Chrome/Edge 桌面版)');
  const handle = await window.showSaveFilePicker({
    suggestedName: 'xbm.db',
    types: [{ description: 'SQLite database', accept: { 'application/x-sqlite3': ['.db'] } }],
  });
  // 切到文件模式:worker 把当前库数据整体迁入新文件库(migrate),然后写盘
  await idbSet(IDB_KEY, handle);
  const r = await store.call('setFileHandle', { handle, migrate: true, name: handle.name });
  await store.call('saveFile');
  return { mode: 'file', name: handle.name, ...r };
}

export async function pickExistingDb(store) {
  if (!pickerSupported) throw new Error('此浏览器不支持选择本地文件(需 Chrome/Edge 桌面版)');
  const [handle] = await window.showOpenFilePicker({
    types: [{ description: 'SQLite database', accept: { 'application/x-sqlite3': ['.db', '.sqlite', '.sqlite3'] } }],
  });
  return connectHandle(store, handle);
}

export async function forgetHandle(store) {
  await idbDel(IDB_KEY);
  return store.call('useOpfsDbOp');
}
