// collector.js — 生成 x.com 页内执行的 Bookmarklet 采集脚本(自包含)。
// 原理:bookmarklet 在 x.com 同源上下文里运行,cookie 自动携带,翻页调 GraphQL,
// 每页数据用 postMessage 回传给打开它的 xbm 页面(opener)。
// 注:X GraphQL 不返回书签时间戳,故采集数据没有 bookmarked_at。

export const COLLECT_ORIGIN = 'https://x.com';
export const COLLECT_URL = 'https://x.com/i/bookmarks';

// 与 Python sync.py 同源的公开 Web Bearer 与 features(x.com 网页请求通用值)
const BEARER = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8Ho81ndTD3tH1d2LO5IkM3LyDdPvrCFbM';

const FEATURES = JSON.stringify({
  graphql_timeline_v2_bookmark_timeline: true,
  rweb_tipjar_consumption_enabled: true,
  responsive_web_graphql_exclude_directive_enabled: true,
  verified_phone_label_enabled: false,
  creator_subscriptions_tweet_preview_api_enabled: true,
  responsive_web_graphql_timeline_navigation_enabled: true,
  responsive_web_graphql_skip_user_profile_image_extensions_enabled: false,
  communities_web_enable_tweet_community_results_fetch: true,
  c9s_tweet_anatomy_moderator_badge_enabled: true,
  articles_preview_enabled: true,
  responsive_web_edit_tweet_api_enabled: true,
  graphql_v1_document_likes_enabled: true,
  freedom_of_speech_not_reach_fetch_enabled: true,
  standardized_nudges_misinfo: true,
  tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled: true,
  rweb_video_timestamps_enabled: true,
  longform_notetweets_rich_text_read_enabled: true,
  longform_notetweets_inline_media_enabled: true,
  responsive_web_enhance_cards_enabled: false,
});

// 采集器源码:字符串模板,{} 转义注意 —— 用单引号拼行避免嵌套反引号
function collectorSource() {
  return [
    '(async () => {',
    '  try {',
    '    const QID = "iblrFnKr6PZUR-dWpfXG6g";',
    '    const BEARER = "' + BEARER + '";',
    '    const FEATURES = ' + FEATURES + ';',
    '    const csrf = (document.cookie.match(/ct0=([^;]+)/) || [])[1] || "";',
    '    const seen = new Set(); let cursor = null; let pages = 0;',
    '    const send = (payload) => { try { window.opener && window.opener.postMessage(payload, "*"); } catch (e) {} };',
    '    const notify = (msg, done) => send({ type: "xbm-status", msg, done: !!done });',
    '    if (!window.opener) { alert("Please launch this from the xbm page (Collect tab)."); return; }',
    '    notify("Collecting bookmarks…", false);',
    '    const norm = (result) => {',
    '      if (!result) return null;',
    '      let r = result;',
    '      if (r.__typename === "TweetWithVisibilityResults") r = r.tweet || null;',
    '      if (!r || !r.legacy) return null;',
    '      const legacy = r.legacy, user = ((r.core || {}).user_results || {}).result || {};',
    '      const u = (user.legacy || {});',
    '      const media = (((legacy.extended_entities || {}).media) || [])',
    '        .filter((m) => m.media_url_https).map((m) => ({ type: m.type || "photo", url: m.media_url_https }));',
    '      const mediaUrls = new Set(media.map((m) => m.url));',
    '      const urls = (((legacy.entities || {}).urls) || [])',
    '        .map((u) => u.expanded_url).filter((u) => u && !mediaUrls.has(u) && u.indexOf("twimg.com") < 0);',
    '      let quoted = "";',
    '      try { quoted = r.quoted_status_result.result.legacy.full_text || ""; } catch (e) {}',
    '      const clean = (s) => (s || "").replace(/https:\\/\\/t\\.co\\/\\w+/g, " ")',
    '        .split("\\n").map((l) => l.replace(/[ \\t]+/g, " ").trim()).filter(Boolean).join("\\n");',
    '      const id = String(r.rest_id || legacy.id_str || "");',
    '      const text = clean(legacy.full_text);',
    '      if (!id || !text) return null;',
    '      const iso = (s) => { try { return new Date(s).toISOString().replace("T", " ").slice(0, 19); } catch (e) { return null; } };',
    '      return { tweet_id: id, text, author_handle: u.screen_name || null, author_name: u.name || null,',
    '        created_at: iso(legacy.created_at), media, urls,',
    '        is_retweet: !!(legacy.retweeted_status_result || (legacy.full_text || "").startsWith("RT @")),',
    '        is_reply: !!legacy.in_reply_to_status_id_str,',
    '        embed_text: "@" + (u.screen_name || "unknown") + " " + text.split("\\n").join(" ") };',
    '    };',
    '    for (let p = 0; p < 500; p++) {',
    '      const vars = { count: 20, includePromotedContent: false };',
    '      if (cursor) vars.cursor = cursor;',
    '      const url = "/i/api/graphql/" + QID + "/Bookmarks?variables=" +',
    '        encodeURIComponent(JSON.stringify(vars)) + "&features=" + encodeURIComponent(JSON.stringify(FEATURES));',
    '      const resp = await fetch(url, { credentials: "include",',
    '        headers: { authorization: "Bearer " + BEARER, "x-csrf-token": csrf, accept: "*/*" } });',
    '      if (!resp.ok) {',
    '        send({ type: "xbm-error", error: "HTTP " + resp.status + " — Bookmarks queryId may have rotated; the collector needs an update." });',
    '        return;',
    '      }',
    '      const data = await resp.json();',
    '      let entries = [];',
    '      try {',
    '        const tl = (data.data.bookmark_timeline_v2 || data.data.bookmark_timeline).timeline;',
    '        for (const inst of tl.instructions || []) entries.push(...(inst.entries || (inst.entry ? [inst.entry] : [])));',
    '      } catch (e) {}',
    '      let added = 0;',
    '      for (const entry of entries) {',
    '        const eid = entry.entryId || "";',
    '        if (eid.startsWith("cursor-bottom")) {',
    '          cursor = ((entry.content || {}).itemContent || {}).value || cursor;',
    '          continue;',
    '        }',
    '        if (!eid.startsWith("tweet-")) continue;',
    '        let result = null;',
    '        try { result = entry.content.itemContent.tweet_results.result; } catch (e) {}',
    '        const row = norm(result);',
    '        if (row && !seen.has(row.tweet_id)) { seen.add(row.tweet_id); added++; send({ type: "xbm-batch", tweets: [row] }); }',
    '      }',
    '      pages++;',
    '      notify("Page " + pages + " fetched, total " + seen.size + " bookmarks", false);',
    '      if (!cursor) break;',
    '      await new Promise((r2) => setTimeout(r2, 1200));',
    '    }',
    '    notify("Finished: " + seen.size + " bookmarks collected", true);',
    '    send({ type: "xbm-batch", tweets: [], done: true });',
    '  } catch (e) {',
    '    try { window.opener.postMessage({ type: "xbm-error", error: String(e) }, "*"); } catch (e2) {}',
    '  }',
    '})();',
  ].join('\n');
}

export function bookmarkletHref() {
  return 'javascript:' + encodeURIComponent(collectorSource());
}

export function collectorLines() {
  return collectorSource().split('\n').length;
}
