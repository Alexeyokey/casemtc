/**
 * Мягкая проверка телефона на клиенте — та же логика, что core/phone.py.
 * Окончательное решение всегда за сервером (ответ invalid_phone).
 */
export function normalizePhone(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const s = raw.trim();
  const hasPlus = s.startsWith("+");
  const digits = s.replace(/\D/g, "");
  if (digits.length === 11 && "78".includes(digits[0])) {
    const body = digits.slice(1);
    return "3489".includes(body[0]) ? `+7${body}` : null;
  }
  if (digits.length === 10 && "3489".includes(digits[0]) && !hasPlus) return `+7${digits}`;
  if (hasPlus && digits.length >= 11 && digits.length <= 15 && !digits.startsWith("7")) return `+${digits}`;
  return null;
}

/** +79990000001 → +7 999 000-00-01 (для показа). */
export function formatPhone(e164: string | null | undefined): string {
  if (!e164) return "";
  const m = /^\+7(\d{3})(\d{3})(\d{2})(\d{2})$/.exec(e164);
  return m ? `+7 ${m[1]} ${m[2]}-${m[3]}-${m[4]}` : e164;
}

export const PHONE_HINT = "Например, +7 916 123-45-67";
