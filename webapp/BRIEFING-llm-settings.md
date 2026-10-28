# 简报：webapp 静态版 LLM 设置区增强 + 两套接口区别定稿

> 给下一个接手 Agent。本次只动了 **webapp 静态版**（不含后端），
> 以及一份两套 LLM 配置接口的区别文档。**服务版（xbm/xbm-v2）本次无改动。**

## 一、背景

用户指出：`xbm-github/webapp/src/llm.js` 那边早就有现成的「自定义 LLM」
设置（base/model 两个输入框 + Save，存 localStorage），本来是配本地 llama-server
用的。本次的活是：把它和服务版新做的设置页对比，取长补短。

## 二、对比结论（已经定稿，别再推翻）

**存储层不合并。** 两套不是重复造轮子，是部署形态硬约束：

|  | 服务版（`xbm serve`） | 静态版（webapp/，Pages 部署） |
|---|---|---|
| 配置存哪 | 服务端：`xbm-v2/.env` footer + `~/.xbm/llm.json` | 浏览器 `localStorage` |
| 谁调 LLM | Python 后端 | 浏览器 JS 直连 |
| 有后端可写配置？ | 有 | **没有**（Pages 纯静态） |

合并 = 让静态版依赖一个不存在的后端。**别动这个结论。**

**合的是"完善度"**：服务版有而静态版没有的（预设快填、测试连接）下沉；
静态版独有的好做法（失败返回 null、`reasoning_content` 捞 JSON、重试梯
2048→4096）**比服务版还细，不要动**。

## 三、本次具体改动

文件（`xbm-github/webapp/` 与源项目 `xbm/webapp/` 两处同步，内容一致）：

| 文件 | 改动 |
|---|---|
| `index.html` | Settings 区 LLM server 段：加预设下拉 `#llm-preset`、加 `Test` 按钮、补说明文字 |
| `src/main.js` | 加 `LLM_PRESETS`（与服务版 `config._PRESET_DEFAULTS` 同源）、`testLlmEndpoint()`、预设 change 处理 |
| `src/llm.js` | **未动**（它的降级/JSON 兜底是优点，保持） |

### 1. 预设快填下拉

```
(presets…)
llama-server (local)     → http://127.0.0.1:8080/v1 + model=local
端砚 Intern-InkStone     → https://discovery-api.intern-ai.org.cn/v1 + Atria-Dawn
SenseNova 商汤           → https://token.sensenova.cn/v1 + sensenova-6.8-flash-lite
```

选中即填入 base/model，提示「prefilled intern — edit, Test, then Save」，
然后下拉归位。`LLM_PRESETS` 与服务端 `xbm_v2/config.py::_PRESET_DEFAULTS`
**同源**——改其中一处要同步另一处。

### 2. Test 按钮（`testLlmEndpoint()`）

发 1-token 探测请求（`max_tokens=32, temperature=0, "reply: OK"`），三种结果：

| 情况 | 文案 |
|---|---|
| 可达 | `✓ chat endpoint reachable (N choice(s), 225ms) — remember Save` |
| 要 key（401/403） | `✗ HTTP 401 — endpoint alive but needs an API key (proxy it, or use a local llama-server)` |
| 不可达 | `✗ unreachable (TypeError): Failed to fetch` |

**为什么静态版测 401 是正常预期**：它部署在 Pages 上，**不带 key**
（公开站点存 key = 泄露，这是有意的）。远程端点要靠代理补 `Authorization`。
UI 文案已写明这条出路，别自作主张把 key 存进 localStorage。

### 3. 踩过的坑（已修，写这里防再犯）

预设下拉的 `<option value>` 一开始写成了
`"intern|https://discovery-api.intern-ai.org.cn/v1|Atria-Dawn"`
（竖线拼参），但 JS 用纯键名 `LLM_PRESETS["intern"]` 查表——**查不到，
快填静默失效**。已改成纯键名（`option value="intern"`），重建后实测通过。
**教训：DOM value 与 JS 查表的键必须一致，靠浏览器实测才能抓到这种静默失效。**

## 四、实测（8798 临时静态服务，已停）

- 预设快填 intern → base/model 正确填入
- Test 端砚：`✗ HTTP 401 — endpoint alive but needs an API key …`（符合预期）
- Save：localStorage `xbm-llm-base/model` 正确写入
- Test 死端口：`✗ unreachable (TypeError): Failed to fetch`

## 五、接手须知

1. **三处一致性**：`xbm/webapp/`（源，有 node_modules 能 build）、
   `xbm-github/webapp/`（镜像）、以及两边的 `dist/` 必须一致。改完流程：
   ```
   cd xbm/webapp && npm run build
   cp -r dist/* ../../xbm-github/webapp/dist/
   ```
   （`xbm-github/webapp` 没有 node_modules，直接那 build 不起来。）
2. **xbm-github 是 git 仓**，本次改动显示为
   `M webapp/index.html` / `M webapp/src/main.js`，**尚未提交**
   （发布是用户来定，别擅自 push）。
3. **服务版本次无改动**——8788 服务要重启才会加载上一次简报里的
   设置页 + ask 进度，那条仍然待办。

## 六、参考

- 两套接口完整对比：`knowledge/summary_xbm_llm_config_two_interfaces.md`
- 服务版设置页做法（上一轮简报）：`xbm-v2/BRIEFING.md`
- 架构与不变量：`xbm-v2/HANDOFF.md`（§2.3 配置衔接）
