// Только для локальной разработки: генерирует initData, подписанный тем же
// MAX_BOT_TOKEN, что лежит в backend/.env. В реальном MAX эту строку
// присылает сам мессенджер через MAX Bridge, а не наш код.
const TEST_BOT_TOKEN = "local-dev-fake-bot-token-for-testing";
const TEST_OWNER_ID = 123456789;

async function hmacSha256(key: BufferSource, message: string): Promise<ArrayBuffer> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  return crypto.subtle.sign("HMAC", cryptoKey, new TextEncoder().encode(message));
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function buildTestInitData(): Promise<string> {
  const authDate = Math.floor(Date.now() / 1000).toString();
  const user = JSON.stringify({ id: TEST_OWNER_ID, first_name: "Test" });

  const pairs: Record<string, string> = { auth_date: authDate, user };
  const checkString = Object.keys(pairs)
    .sort()
    .map((key) => `${key}=${pairs[key]}`)
    .join("\n");

  const secret = await hmacSha256(new TextEncoder().encode("WebAppData"), TEST_BOT_TOKEN);
  const hashBuffer = await hmacSha256(secret, checkString);
  const hash = toHex(hashBuffer);

  return Object.keys(pairs)
    .sort()
    .map((key) => `${key}=${encodeURIComponent(pairs[key])}`)
    .join("&") + `&hash=${hash}`;
}