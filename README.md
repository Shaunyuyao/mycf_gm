## GSMArena 抓取完整方案（已验证可用）

### 结论（推荐主流程）
- 最稳定方案：`browser_only_scraper.py + CDP 复用你手动可访问的 Chrome 会话`
- 原因：浏览器可访问并不代表独立 HTTP 客户端可访问。复用同一浏览器上下文最稳。

### 0) 安装
```bash
uv sync
uv run playwright install chromium
```

### 1) 启动可被脚本连接的 Chrome（9222）
先关闭所有 Chrome：
```bat
taskkill /F /IM chrome.exe
```

启动 CDP 实例（Windows）：
```bat
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome-cdp-gsm-9222"
```

验证 9222 端口：
```bat
curl http://127.0.0.1:9222/json/version
```
能返回 JSON 即正常。

### 2) 在该 Chrome 手动通过验证
- 在这一个 CDP Chrome 中访问：`https://www.gsmarena.com/makers.php3`
- 若出现 Turnstile，手动完成验证并进入正常页面。

### 3) 运行 browser-only 抓取
小批量验证：
```bash
uv run python browser_only_scraper.py --cdp-url http://127.0.0.1:9222 --limit 3 --timeout-seconds 600 
```

推荐批量参数（带重试）：
```bash
uv run python browser_only_scraper.py --cdp-url http://127.0.0.1:9222 --limit 20 --timeout-seconds 600 --max-retries 5 --retry-wait-seconds 4 --max-retry-wait-seconds 120 --interval-seconds 5 --jitter-ratio 0.3
```

长跑稳定参数（断点续跑 + 失败队列补抓）：
```bash
uv run python browser_only_scraper.py --cdp-url http://127.0.0.1:9222 --limit 0 --timeout-seconds 600 --max-retries 5 --retry-wait-seconds 4 --max-retry-wait-seconds 120 --interval-seconds 5 --jitter-ratio 0.3 --resume --failed-retry-rounds 2
```

输出文件：
- `data/gsmarena_specs_browser_only.json`
- `data/gsmarena_browser_only_checkpoint.json`
- `data/gsmarena_failed_urls.txt`

### 4) 全量抓取建议
```bash
uv run python browser_only_scraper.py --cdp-url http://127.0.0.1:9222 --limit 0 --timeout-seconds 600 --interval-seconds 20 --jitter-ratio 0.1 --max-retries 5 --retry-wait-seconds 4 --max-retry-wait-seconds 120 --resume --failed-retry-rounds 2

```

### 常见问题与处理
- `ECONNREFUSED 127.0.0.1:9222`  
  说明 CDP Chrome 没启动成功，重做“步骤 1”。

- `ERR_CONNECTION_CLOSED`  
  瞬时网络错误，使用 `--max-retries 3` 即可自动恢复。

- 浏览器手动能访问，但脚本显示 `TURNSTILE`  
  通常是没有复用到同一个会话。必须使用 `--cdp-url` 连接你已验证的 Chrome。

- `DEP0169` Warning  
  为 Playwright 内部 Node 警告，可忽略，不影响抓取。
