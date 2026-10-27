// llm.js — 可选 LLM 客户端(OpenAI 兼容 /v1/chat/completions;llama-server、远程 API 均可)
// 配置存 localStorage。所有函数失败时返回 null(调用方决定降级),不抛出。

export function llmConfig() {
  return {
    base: (localStorage.getItem('xbm-llm-base') || '').trim(),
    model: (localStorage.getItem('xbm-llm-model') || 'local').trim(),
  };
}

async function chat(base, model, messages, maxTokens) {
  const resp = await fetch(base.replace(/\/$/, '') + '/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, temperature: 0.3, max_tokens: maxTokens, messages }),
  });
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  const data = await resp.json();
  return data.choices?.[0]?.message || {};
}

// 思考型模型兜底:content 空/无 JSON 时从 reasoning_content 捞
function extractJson(msg) {
  const tryParse = (s) => { try { return JSON.parse(s); } catch { return null; } };
  let text = (msg.content || '').trim();
  let d = tryParse(text);
  if (!d) {
    const m = (text + '\n' + (msg.reasoning_content || '')).match(/\{[\s\S]*\}/);
    if (m) d = tryParse(m[0]);
  }
  return d;
}

// ---- 簇命名(JSON 约束 few-shot,重试梯 2048→4096;与 Python 版同款模板) ----
export async function nameCluster(base, model, texts) {
  for (const maxTokens of [2048, 4096]) {
    try {
      const msg = await chat(base, model, [
        { role: 'system', content: '给一组同主题推文起简短中文类目名。只输出JSON:{"name":"类目名"}。' },
        { role: 'user', content: '推文:\nllama.cpp MTP 解码加速\n本地显存优化\n输出JSON:' },
        { role: 'assistant', content: '{"name":"本地推理优化"}' },
        { role: 'user', content: '推文:\n京都红叶季攻略\n东京咖啡店打卡\n输出JSON:' },
        { role: 'assistant', content: '{"name":"日本旅行"}' },
        { role: 'user', content: '推文:\n' + texts.slice(0, 8).join('\n---\n') + '\n输出JSON:' },
      ], maxTokens);
      const d = extractJson(msg);
      const name = (d?.name || '').trim().slice(0, 24);
      if (name) return name;
    } catch { /* retry / give up */ }
  }
  return null;
}

// ---- 时间脉络叙事(14 行/段分段,重试梯) ----
export async function narrative(base, model, digestText) {
  const lines = digestText.split('\n');
  const parts = [];
  const SYS = '你根据用户每月的书签主题统计,梳理他关注焦点随时间演变的脉络。' +
    '用中文按时间顺序写:每期一两句(指出兴起/消退/转移的主题),结尾用一段总结整体演变主线。直接输出正文。';
  for (let i = 0; i < lines.length; i += 14) {
    const seg = lines.slice(i, i + 14).join('\n');
    let done = false;
    for (const maxTokens of [2048, 4096]) {
      try {
        const msg = await chat(base, model, [
          { role: 'system', content: SYS },
          { role: 'user', content: '书签月度统计:\n' + seg },
        ], maxTokens);
        const text = (msg.content || '').trim();
        if (text) { parts.push(text.slice(0, 4000)); done = true; break; }
      } catch { /* retry */ }
    }
    if (!done && parts.length === 0) throw new Error('model returned no narrative');
  }
  return parts.join('\n').slice(0, 8000);
}

// ---- 线索卡片 ----
export async function threadCard(base, model, prompt) {
  for (const maxTokens of [2048, 3072]) {
    try {
      const msg = await chat(base, model, [
        { role: 'system', content: '你在为用户的知识线索写卡片。只输出JSON:' +
          '{"about":"2-3句:这条线索收集的是什么、围绕什么问题",' +
          '"next":"2-3句:下一步深入的具体建议,基于已收集内容指出缺口"}' },
        { role: 'user', content: '线索「本地大模型推理加速」:共 120 条,活跃 10 个月\n' +
          '代表内容:\n- llama.cpp MTP 解码加速\n- 显存优化技巧\n输出JSON:' },
        { role: 'assistant', content: '{"about":"围绕在消费级硬件上跑大模型的推理加速技术,覆盖解码优化与显存管理。","next":"建议系统梳理量化方案对比,并实测 MTP+KV 缓存组合在本机的收益。"}' },
        { role: 'user', content: prompt + '\n输出JSON:' },
      ], maxTokens);
      const d = extractJson(msg);
      if (d?.about || d?.next) {
        return { about: (d.about || '').slice(0, 400), next: (d.next || '').slice(0, 400) };
      }
    } catch { /* retry */ }
  }
  return null;
}
