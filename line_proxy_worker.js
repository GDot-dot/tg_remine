// Cloudflare Worker：LINE 商店頁面代理
// 部署後將 URL 和 SECRET 設定到 fly.io secrets

const ALLOWED_HOST = "store.line.me";

export default {
  async fetch(request, env, ctx) {
    // 驗證 secret（防止被陌生人濫用）
    const url = new URL(request.url);
    const secret = url.searchParams.get("secret");
    if (env.PROXY_SECRET && secret !== env.PROXY_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    // 取得目標 URL
    const target = url.searchParams.get("url");
    if (!target) {
      return new Response("Missing ?url= parameter", { status: 400 });
    }

    // 只允許 LINE 商店
    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return new Response("Invalid URL", { status: 400 });
    }
    if (targetUrl.hostname !== ALLOWED_HOST) {
      return new Response("Only store.line.me is allowed", { status: 403 });
    }

    // 轉發請求，帶上瀏覽器 headers
    const response = await fetch(target, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
          "AppleWebKit/537.36 (KHTML, like Gecko) " +
          "Chrome/124.0.0.0 Safari/537.36",
        Accept:
          "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        Connection: "keep-alive",
        "Upgrade-Insecure-Requests": "1",
      },
    });

    const html = await response.text();
    return new Response(html, {
      status: response.status,
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
