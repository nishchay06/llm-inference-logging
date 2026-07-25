// Formatting + status constants (presentation), ported from the design.

export function nfmt(n: number | null | undefined): string {
  return (n == null ? 0 : n).toLocaleString("en-US");
}
export function msFmt(n: number | null | undefined): string {
  return n == null ? "—" : nfmt(Math.round(n)) + " ms";
}
export function pctFmt(x: number): string {
  return (x * 100).toFixed(1) + "%";
}
export function timeShort(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
export function bucketLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
}
export function fullTime(iso: string): string {
  return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const STATUS: Record<string, { color: string; label: string }> = {
  success: { color: "#0d9488", label: "Success" },
  error: { color: "#dc2626", label: "Error" },
  cancelled: { color: "#d97706", label: "Cancelled" },
};
export const MONO = "ui-monospace, SFMono-Regular, Menlo, monospace";
