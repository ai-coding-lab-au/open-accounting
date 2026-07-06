import { AxiosError } from "axios";

export function apiErrorMessage(err: unknown, fallback = "Request failed"): string {
  if (err instanceof AxiosError) {
    const detail = err.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (typeof first?.msg === "string") return first.msg;
    }
    if (typeof err.message === "string" && err.message) return err.message;
  }
  if (err instanceof Error) return err.message || fallback;
  if (typeof err === "string") return err;
  return fallback;
}
