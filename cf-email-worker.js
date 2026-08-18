// Cloudflare Email Worker — receives email, extracts OTP, stores in KV
//
// SETUP (Cloudflare Dashboard):
//   1. Workers & Pages → Create Worker → paste this script
//   2. Settings → Bindings → add KV namespace binding:
//        Variable name: OTP_STORE
//        KV namespace:  (select the "otp-store" namespace you created)
//   3. Settings → Triggers → Email Routes:
//        Destination:  @dailykhabar.cfd
//        Action:       Send to a Worker → select this worker
//
// This worker fires when an email arrives at *@dailykhabar.cfd.
// It extracts the numeric confirmation code and stores it in KV
// keyed by the recipient address, with a 5-minute TTL.

export default {
  async email(message, env, ctx) {
    const recipient = message.to;
    const rawBody = await new Response(message.raw).text();

    // Facebook confirmation emails contain a numeric code.
    // Match common patterns: "code is 12345", "code: 12345", standalone 5-digit
    const codeMatch = rawBody.match(/(?:code[:\s]+|is\s+)(\d{5,8})/i)
                   || rawBody.match(/(\d{5,8})/);

    if (!codeMatch) {
      console.log(`No code found in email to ${recipient}`);
      return;
    }

    const code = codeMatch[1];
    const key = `otp:${recipient}`;
    const record = JSON.stringify({ code, ts: Date.now() });

    // Store in KV with 5-minute TTL so stale codes auto-expire
    await env.OTP_STORE.put(key, record, { expirationTtl: 300 });

    console.log(`Stored OTP for ${recipient}: ${code}`);
  }
};
