// Cloudflare HTTP Worker — serves OTP codes via API
//
// SETUP (Cloudflare Dashboard):
//   1. Workers & Pages → Create Worker → paste this script
//   2. Settings → Bindings → add KV namespace binding:
//        Variable name: OTP_STORE
//        KV namespace:  (select the "otp-store" namespace you created)
//   3. Settings → Variables → add Environment Variable:
//        API_KEY = (pick a random secret string, e.g. "k9x2m5p8q3w7")
//
// API:
//   GET /otp?email=user@dailykhabar.cfd
//   Header: Authorization: Bearer <API_KEY>
//
//   200 OK:     { "code": "12345", "ts": 1234567890 }
//   202 Pending: { "error": "not_ready", "message": "waiting for email" }
//   401:         { "error": "unauthorized" }
//   400:         { "error": "bad_request", "message": "missing email param" }

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "Authorization",
        },
      });
    }

    // --- Auth ---
    const auth = request.headers.get("Authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    if (!env.API_KEY || token !== env.API_KEY) {
      return jsonResp({ error: "unauthorized" }, 401);
    }

    // --- Route ---
    if (url.pathname === "/otp" && request.method === "GET") {
      const email = url.searchParams.get("email");
      if (!email) {
        return jsonResp({ error: "bad_request", message: "missing email param" }, 400);
      }

      const key = `otp:${email}`;
      const raw = await env.OTP_STORE.get(key);

      if (!raw) {
        return jsonResp({ error: "not_ready", message: "waiting for email" }, 202);
      }

      try {
        const record = JSON.parse(raw);
        const ageMs = Date.now() - (record.ts || 0);

        // If older than 3 minutes, treat as stale
        if (ageMs > 3 * 60 * 1000) {
          await env.OTP_STORE.delete(key);
          return jsonResp({ error: "not_ready", message: "code expired" }, 202);
        }

        return jsonResp({ code: record.code, ts: record.ts });
      } catch {
        // If KV value is a plain string (legacy), return it directly
        return jsonResp({ code: raw, ts: Date.now() });
      }
    }

    return jsonResp({ error: "not_found" }, 404);
  }
};

function jsonResp(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
