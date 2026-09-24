/**
 * 时间展示工具（DESIGN.md §8：界面统一 Asia/Shanghai，悬浮显示相对时间）。
 *
 * 为什么必须走这里而不是各页面 `new Date(x).toLocaleString()`：
 * 后端存的是 **naive UTC**（`app/core/timeutil.py`），返回的 ISO8601 **不带时区后缀**
 * （如 `2026-09-24T13:43:55`）。浏览器会把这种字符串当作**本地时间**解析，
 * 于是"案件触发时间"在东八区会平白少 8 小时 —— 演示时表现为"刚触发的事件显示成下午"。
 * 因此统一用 `dayjs.utc(值)` 显式按 UTC 解释，再用固定偏移 +8 转成北京时间。
 */
import dayjs from "dayjs";
import utc from "dayjs/plugin/utc";
import relativeTime from "dayjs/plugin/relativeTime";

dayjs.extend(utc);
dayjs.extend(relativeTime);

/** 东八区固定偏移（小时）。用固定偏移而非 IANA 时区，是为了不引入 timezone 插件与本地 tz 数据。 */
const CST_OFFSET_HOURS = 8;

/** 把后端返回的 naive UTC 字符串转成 dayjs 对象（北京时区视角）。 */
export function toCst(iso: string | null | undefined): dayjs.Dayjs | null {
  if (!iso) return null;
  return dayjs.utc(iso).utcOffset(CST_OFFSET_HOURS);
}

/** 统一时间格式：`YYYY-MM-DD HH:mm:ss`；空值统一显示 `-`（不显示 Invalid Date）。 */
export function formatDateTime(iso: string | null | undefined): string {
  const value = toCst(iso);
  return value ? value.format("YYYY-MM-DD HH:mm:ss") : "-";
}

/** 悬浮提示用的相对时间（如"3 分钟前"），失败时回退到绝对时间。 */
export function formatRelative(iso: string | null | undefined): string {
  const value = toCst(iso);
  if (!value) return "-";
  try {
    return value.fromNow();
  } catch {
    return value.format("YYYY-MM-DD HH:mm:ss");
  }
}

/**
 * 分数展示：统一保留 2 位小数（DESIGN.md §8）。
 *
 * 与 `toFixed` 的区别：null / undefined / NaN 一律显示 `-`，避免界面上出现 "NaN"
 * 这种"看起来是数据问题"的噪声 —— 模型分在模型未参与决策时就是空值，属于正常状态。
 */
export function formatScore(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(digits);
}

/** 带符号的贡献值（如 `+1.29` / `-0.77`），模型贡献榜专用。 */
export function formatSigned(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

/** 金额展示（人民币符号 + 千分位，保留 2 位）。 */
export function formatAmount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
