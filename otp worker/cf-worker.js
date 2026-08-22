// Cloudflare Worker — OTP receive + serve (single worker)
//
// SETUP (Cloudflare Dashboard — one worker only):
//   1. Workers & Pages → Create Worker → paste this entire script
//   2. Settings → Bindings → add KV namespace binding:
//        Variable name: OTP_STORE
//        KV namespace:  (select the "otp-store" namespace you created)
//   3. Settings → Variables → add Environment Variable:
//        API_KEY = (pick a random secret string, e.g. "k9x2m5p8q3w7")
//   4. Settings → Triggers → Email Routes:
//        Destination:  @dailykhabar.cfd
//        Action:       Send to a Worker → select this worker
//
// HANDLERS:
//   email  — fires when an email arrives at *@dailykhabar.cfd
//            extracts the numeric code and stores it in KV (5-min TTL)
//
//   fetch  — HTTP API that the Python script polls
//            GET /otp?email=user@dailykhabar.cfd
//            Header: Authorization: Bearer <API_KEY>
//
//            200 OK:      { "code": "12345", "ts": 1234567890 }
//            202 Pending: { "error": "not_ready" }
//            401:         { "error": "unauthorized" }
//            400:         { "error": "bad_request" }

export default {
  // ---------------------------------------------------------- email handler
  async email(message, env, ctx) {
    const recipient = message.to;

    const subject = message.headers.get("subject") || "";
    const rawBody = await new Response(message.raw).text();

    // drop the headers: the body begins after the first blank line
    let body = rawBody;
    const sep = rawBody.indexOf("\r\n\r\n");
    if (sep >= 0) body = rawBody.slice(sep + 4);

    // decode simple quoted-printable soft/hard breaks that could split digits
    body = body.replace(/=\r?\n/g, "").replace(/=([0-9A-Fa-f]{2})/g,
      (_, h) => String.fromCharCode(parseInt(h, 16)));

    const from = (message.from || "").toLowerCase();
    const subjLow = subject.toLowerCase();
    const isMicrosoft =
      from.includes("microsoft") || from.includes("account-security") ||
      subjLow.includes("microsoft") || subjLow.includes("security code");

    let code = null;

    if (isMicrosoft) {
      // Outlook/Microsoft security codes are 4-8 digits and ALWAYS sit in
      // an explicit code context ("Security code: 759954"). Never bare-scan
      // this mail: its own footer carries the Redmond ZIP "98052", which
      // the old generic 5-digit scan happily returned as a bogus OTP.
      const m = subject.match(/(?<!\d)(\d{4,8})(?!\d)/)
             || body.match(/security\s*code[^0-9]{0,20}(?<!\d)(\d{4,8})(?!\d)/i)
             || body.match(/code[^0-9]{0,30}(?<!\d)(\d{4,8})(?!\d)/i);
      if (m) {
        code = m[1];
        console.log(`Microsoft code ${code} for ${recipient}`);
      } else {
        console.log(`No Microsoft security code found in email to ${recipient}`);
      }
    } else {
      // Facebook signup codes: exactly five digits, subject first.
      // NEVER scan message.raw for bare digit-runs otherwise — the RFC822
      // headers are full of 13-digit DKIM/ARC timestamps.
      const subjMatch = subject.match(/(?<!\d)(\d{5})(?!\d)/);
      if (subjMatch) {
        code = subjMatch[1];
        console.log(`Code ${code} taken from subject for ${recipient}`);
      }

      if (!code) {
        // prefer an explicit "code" context, else a standalone 5-digit run
        // ((?<!\d)(?!\d)) so parts of longer IDs/timestamps never match
        const m = body.match(/code[^0-9]{0,30}(?<!\d)(\d{5})(?!\d)/i)
               || body.match(/(?<!\d)(\d{5})(?!\d)/);
        if (m) {
          code = m[1];
          console.log(`Code ${code} taken from body for ${recipient}`);
        } else {
          console.log(`No 5-digit code found in email to ${recipient}`);
        }
      }
    }

    if (!code) {
      return;
    }

    const key = `otp:${recipient}`;
    const record = JSON.stringify({ code, ts: Date.now() });

    // Store in KV with 5-minute TTL so stale codes auto-expire
    await env.OTP_STORE.put(key, record, { expirationTtl: 300 });
    console.log(`Stored OTP for ${recipient}: ${code}`);
  },

  // ---------------------------------------------------------- fetch handler
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
      const emailAddr = url.searchParams.get("email");
      if (!emailAddr) {
        return jsonResp({ error: "bad_request", message: "missing email param" }, 400);
      }

      const key = `otp:${emailAddr}`;
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
  },
};

// --------------------------------------------------------------- helpers
function jsonResp(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
