import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useCompanyStore } from "../store/company";
import { displayName, formatDate, formatMoney } from "../lib/format";
import { apiErrorMessage } from "../lib/errors";
import { toast } from "../lib/toast";
import { DateInput } from "../components/ui/DateInput";
import { toLocalIsoDate } from "../lib/date";
import type {
  BalanceSheetReport,
  BankAccountWithBalance,
  BankStatement,
  BASReport,
  GSTExposureReport,
  PnLReport,
  TrialBalanceReport,
} from "../types/api";

type Tab =
  | "trial-balance"
  | "balance-sheet"
  | "pnl"
  | "bank-statement"
  | "gst"
  | "bas";

const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

const TABS: { id: Tab; label: string; subtitle?: string }[] = [
  {
    id: "trial-balance",
    label: "Trial balance",
    subtitle:
      "Per-account debit/credit totals as of a date. The Dr/Cr totals balance when every bank txn has been categorised.",
  },
  {
    id: "balance-sheet",
    label: "Balance sheet",
    subtitle:
      "Assets / Liabilities / Equity at a point in time, with retained earnings rolled in.",
  },
  {
    id: "pnl",
    label: "Profit & Loss",
    subtitle:
      "Income, COGS and expenses from business-account transactions plus journal entries.",
  },
  {
    id: "bank-statement",
    label: "Bank statement",
    subtitle: "Monthly per-account in/out with running balance.",
  },
  {
    id: "gst",
    label: "GST exposure",
    subtitle:
      "Per-box breakdown of taxable / GST-free / input-taxed activity. Drives BAS.",
  },
  {
    id: "bas",
    label: "BAS",
  },
];


export default function ReportsPage() {
  const currentId = useCompanyStore((s) => s.currentId);
  const [tab, setTab] = useState<Tab>("trial-balance");

  if (!currentId) {
    return (
      <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
        <h2 className="font-semibold">No company selected</h2>
        <p className="text-sm text-slate-500 mt-1">Pick a company in the top bar.</p>
      </div>
    );
  }

  const active = TABS.find((t) => t.id === tab)!;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Reports</h1>

      <div className="flex flex-wrap gap-1 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-sm border-b-2 whitespace-nowrap ${
              t.id === tab
                ? "border-emerald-600 text-emerald-700 font-medium"
                : "border-transparent text-slate-600 hover:text-slate-900"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab !== "bas" && active.subtitle && (
        <p className="text-xs text-slate-500 -mt-2">{active.subtitle}</p>
      )}

      {tab === "trial-balance" && <TrialBalanceTab />}
      {tab === "balance-sheet" && <BalanceSheetTab />}
      {tab === "pnl" && <PnLTab />}
      {tab === "bank-statement" && <BankStatementTab />}
      {tab === "gst" && <GSTTab />}
      {tab === "bas" && <BASTab />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function downloadPdf(path: string, params: Record<string, string | number>) {
  // Use axios so the X-Company-Id header is injected via the same interceptor
  // as the rest of the app; a plain <a download> can't set headers.
  api
    .get<Blob>(path, { params, responseType: "blob" })
    .then((r) => {
      const url = URL.createObjectURL(r.data);
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    })
    .catch((e) => toast(`PDF failed: ${apiErrorMessage(e)}`, "error"));
}

function Card({
  title,
  children,
  right,
}: {
  title: string;
  children: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <section className="bg-surface rounded-lg border border-slate-200">
      <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between gap-2">
        <h2 className="font-semibold text-sm">{title}</h2>
        {right}
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

function fyLabel(year: number): string {
  return "FY " + (year - 1) + "-" + year;
}

function fyOptions(centerYear: number): number[] {
  return Array.from(new Set([centerYear - 1, centerYear, centerYear + 1])).sort();
}

function KV({
  label,
  value,
  emphasise,
}: {
  label: string;
  value: React.ReactNode;
  emphasise?: boolean;
}) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div
        className={`mt-0.5 ${
          emphasise ? "text-xl font-semibold" : "text-sm font-medium"
        }`}
      >
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Bank statement
// ---------------------------------------------------------------------------

async function fetchBankAccounts(): Promise<BankAccountWithBalance[]> {
  const { data } = await api.get<BankAccountWithBalance[]>("/bank-accounts");
  return data;
}

async function fetchBankStatement(
  bankAccountId: number,
  year: number,
  month: number,
): Promise<BankStatement> {
  const { data } = await api.get<BankStatement>("/reports/bank-statement", {
    params: { bank_account_id: bankAccountId, year, month },
  });
  return data;
}

function BankStatementTab() {
  const currentId = useCompanyStore((s) => s.currentId);
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1);

  const { data: accounts } = useQuery({
    queryKey: ["bank-accounts", currentId],
    queryFn: fetchBankAccounts,
    enabled: !!currentId,
  });
  const [bankAccountId, setBankAccountId] = useState<number | null>(null);
  const effectiveId = bankAccountId ?? accounts?.[0]?.id ?? null;

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["report-bank-statement", currentId, effectiveId, year, month],
    queryFn: () => fetchBankStatement(effectiveId!, year, month),
    enabled: !!effectiveId && !!currentId,
  });

  return (
    <div className="space-y-4">
      <Card
        title="Filters"
        right={
          effectiveId && data ? (
            <button
              className="btn-secondary text-xs"
              onClick={() =>
                downloadPdf("/reports/bank-statement/pdf", {
                  bank_account_id: effectiveId,
                  year,
                  month,
                })
              }
            >
              Download PDF
            </button>
          ) : null
        }
      >
        <div className="grid grid-cols-3 gap-3 text-sm">
          <label className="block">
            <span className="block text-slate-600 mb-1">Account</span>
            <select
              className="input"
              value={effectiveId ?? ""}
              onChange={(e) => setBankAccountId(Number(e.target.value))}
            >
              {(accounts ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="block text-slate-600 mb-1">Year</span>
            <input
              type="number"
              className="input"
              min={2000}
              max={2100}
              value={year}
              onChange={(e) => setYear(Number(e.target.value))}
            />
          </label>
          <label className="block">
            <span className="block text-slate-600 mb-1">Month</span>
            <select
              className="input"
              value={month}
              onChange={(e) => setMonth(Number(e.target.value))}
            >
              {Array.from({ length: 12 }, (_, i) => (
                <option key={i + 1} value={i + 1}>
                  {MONTH_NAMES[i]} {year}
                </option>
              ))}
            </select>
          </label>
        </div>
      </Card>

      {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {isError && (
        <p className="text-sm text-rose-600">
          {apiErrorMessage(error)}
        </p>
      )}
      {data && <BankStatementBody data={data} />}
    </div>
  );
}

function BankStatementBody({ data }: { data: BankStatement }) {
  return (
    <div className="space-y-4">
      <Card title={`${data.bank_account_name} · ${formatDate(data.period_start)} → ${formatDate(data.period_end)}`}>
        <div className="grid grid-cols-4 gap-4">
          <KV label="Opening balance" value={formatMoney(data.opening_balance)} emphasise />
          <KV label="Total in" value={<span className="text-emerald-700">{formatMoney(data.total_in)}</span>} emphasise />
          <KV label="Total out" value={<span className="text-rose-700">{formatMoney(data.total_out)}</span>} emphasise />
          <KV label="Closing balance" value={formatMoney(data.closing_balance)} emphasise />
        </div>
      </Card>

      <Card title="Movements">
        {data.rows.length === 0 ? (
          <p className="text-sm text-slate-500">No transactions in this period.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-600 bg-slate-50">
                <tr>
                  <th className="text-left px-3 py-2">Date</th>
                  <th className="text-left px-3 py-2">Description</th>
                  <th className="text-left px-3 py-2">Category</th>
                  <th className="text-right px-3 py-2">In</th>
                  <th className="text-right px-3 py-2">Out</th>
                  <th className="text-right px-3 py-2">Balance</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <tr key={r.id} className="border-t">
                    <td className="px-3 py-2 text-xs whitespace-nowrap">{formatDate(r.occurred_at)}</td>
                    <td className="px-3 py-2 text-xs">
                      {r.memo ?? displayName(r.counter_party_name, "provider")}
                    </td>
                    <td className="px-3 py-2 text-xs">
                      {r.account_code ? (
                        <span>
                          <span className="font-mono text-slate-500">{r.account_code}</span>{" "}
                          {r.account_name}
                        </span>
                      ) : (
                        <span className="text-amber-700">Uncategorised</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right text-emerald-700">
                      {r.direction === "in" ? formatMoney(r.amount) : ""}
                    </td>
                    <td className="px-3 py-2 text-right text-rose-700">
                      {r.direction === "out" ? formatMoney(r.amount) : ""}
                    </td>
                    <td className="px-3 py-2 text-right font-mono">
                      {formatMoney(r.running_balance)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: P&L
// ---------------------------------------------------------------------------

async function fetchPnL(start: string, end: string): Promise<PnLReport> {
  const { data } = await api.get<PnLReport>("/reports/profit-loss", {
    params: { period_start: start, period_end: end },
  });
  return data;
}

function PnLTab() {
  const currentId = useCompanyStore((s) => s.currentId);
  // Default to AU FY-to-date (Jul 1 of last cycle → today).
  const today = useMemo(() => new Date(), []);
  const defaultStart = useMemo(() => {
    const y = today.getMonth() < 6 ? today.getFullYear() - 1 : today.getFullYear();
    return `${y}-07-01`;
  }, [today]);
  const defaultEnd = toLocalIsoDate(today);

  const [start, setStart] = useState(defaultStart);
  const [end, setEnd] = useState(defaultEnd);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["report-pnl", currentId, start, end],
    queryFn: () => fetchPnL(start, end),
    enabled: !!currentId,
  });

  return (
    <div className="space-y-4">
      <Card
        title="Period"
        right={
          data ? (
            <button
              className="btn-secondary text-xs"
              onClick={() =>
                downloadPdf("/reports/profit-loss/pdf", {
                  period_start: start,
                  period_end: end,
                })
              }
            >
              Download PDF
            </button>
          ) : null
        }
      >
        <div className="grid grid-cols-3 gap-3 text-sm">
          <label className="block">
            <span className="block text-slate-600 mb-1">From</span>
            <DateInput className="input pr-7" value={start} onChange={setStart} />
          </label>
          <label className="block">
            <span className="block text-slate-600 mb-1">To</span>
            <DateInput className="input pr-7" value={end} onChange={setEnd} />
          </label>
          <div className="text-xs text-slate-500 self-end pb-2">
            Default: this AU financial year to date.
          </div>
        </div>
      </Card>

      {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {isError && (
        <p className="text-sm text-rose-600">
          {apiErrorMessage(error)}
        </p>
      )}
      {data && (
        <>
          <Card title="Summary">
            <div className="grid grid-cols-4 gap-4">
              <KV label="Income" value={formatMoney(data.total_income)} emphasise />
              <KV label="COGS" value={formatMoney(data.total_cogs)} emphasise />
              <KV label="Expenses" value={formatMoney(data.total_expense)} emphasise />
              <KV
                label="Net profit"
                emphasise
                value={
                  <span
                    className={
                      Number(data.net_profit) < 0
                        ? "text-rose-700"
                        : "text-emerald-700"
                    }
                  >
                    {formatMoney(data.net_profit)}
                  </span>
                }
              />
            </div>
            {(Number(data.uncategorised_in) > 0 ||
              Number(data.uncategorised_out) > 0) && (
              <p className="mt-4 text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded p-2">
                Uncategorised: {formatMoney(data.uncategorised_in)} in,{" "}
                {formatMoney(data.uncategorised_out)} out. Set a category on
                those transactions to include them in the P&amp;L.
              </p>
            )}
          </Card>

          <PnLSection title="Income" rows={data.income_rows} total={data.total_income} />
          {data.cogs_rows.length > 0 && (
            <PnLSection title="Cost of Sales" rows={data.cogs_rows} total={data.total_cogs} />
          )}
          <PnLSection title="Expenses" rows={data.expense_rows} total={data.total_expense} />
        </>
      )}
    </div>
  );
}

function PnLSection({
  title,
  rows,
  total,
}: {
  title: string;
  rows: PnLReport["income_rows"];
  total: string;
}) {
  return (
    <Card title={title}>
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500">No activity in this period.</p>
      ) : (
        <table className="w-full text-sm">
          <tbody>
            {rows.map((r) => (
              <tr key={r.account_id} className="border-b last:border-b-0">
                <td className="py-1.5 pr-3">
                  <span className="font-mono text-slate-500 mr-2">{r.code}</span>
                  {r.name}
                </td>
                <td className="py-1.5 text-right font-mono">{formatMoney(r.total)}</td>
              </tr>
            ))}
            <tr>
              <td className="pt-2 font-semibold">Total {title.toLowerCase()}</td>
              <td className="pt-2 text-right font-mono font-semibold">
                {formatMoney(total)}
              </td>
            </tr>
          </tbody>
        </table>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Tab: BAS
// ---------------------------------------------------------------------------

async function fetchBAS(fyYear: number, quarter: number): Promise<BASReport> {
  const { data } = await api.get<BASReport>("/reports/bas", {
    params: { fy_year: fyYear, quarter },
  });
  return data;
}

function BASTab() {
  const currentId = useCompanyStore((s) => s.currentId);
  const today = new Date();
  const defaultFy = today.getMonth() < 6 ? today.getFullYear() : today.getFullYear() + 1;
  const defaultQ = Math.ceil(((today.getMonth() + 6) % 12 + 1) / 3);
  const [fyYear, setFyYear] = useState(defaultFy);
  const [quarter, setQuarter] = useState(defaultQ);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["report-bas", currentId, fyYear, quarter],
    queryFn: () => fetchBAS(fyYear, quarter),
    enabled: !!currentId,
  });

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500 -mt-2">
        {data?.gst_registered === false
          ? "Quarterly GST return. Placeholder while the firm is not GST-registered."
          : "Quarterly GST return for the period."}
      </p>
      <Card
        title="Period"
        right={
          data ? (
            <button
              className="btn-secondary text-xs"
              onClick={() =>
                downloadPdf("/reports/bas/pdf", {
                  fy_year: fyYear,
                  quarter,
                })
              }
            >
              Download PDF
            </button>
          ) : null
        }
      >
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="block min-w-40">
            <span className="block text-slate-600 mb-1">Financial year</span>
            <select
              className="input"
              value={fyYear}
              onChange={(e) => setFyYear(Number(e.target.value))}
            >
              {fyOptions(fyYear).map((year) => (
                <option key={year} value={year}>
                  {fyLabel(year)}
                </option>
              ))}
            </select>
          </label>
          <label className="block min-w-40">
            <span className="block text-slate-600 mb-1">Quarter</span>
            <select
              className="input"
              value={quarter}
              onChange={(e) => setQuarter(Number(e.target.value))}
            >
              <option value={1}>Q1 (Jul–Sep)</option>
              <option value={2}>Q2 (Oct–Dec)</option>
              <option value={3}>Q3 (Jan–Mar)</option>
              <option value={4}>Q4 (Apr–Jun)</option>
            </select>
          </label>
        </div>
      </Card>

      {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {isError && (
        <p className="text-sm text-rose-600">
          {apiErrorMessage(error)}
        </p>
      )}
      {data && (
        <>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-slate-600">GST registered:</span>
            <span
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                data.gst_registered
                  ? "bg-emerald-100 text-emerald-800 border border-emerald-200"
                  : "bg-amber-100 text-amber-900 border border-amber-200"
              }`}
            >
              {data.gst_registered ? "Yes" : "No"}
            </span>
          </div>
          {!data.gst_registered && (
            <div className="text-sm bg-amber-50 border border-amber-200 text-amber-900 rounded p-3">
              <strong>Not GST-registered.</strong> All GST fields below are
              shown as zero. Numbers will populate automatically once GST is
              registered and transactions are recorded with a GST portion.
            </div>
          )}
          {data.uncategorised_count > 0 && (
            <div className="text-sm bg-amber-50 border border-amber-200 text-amber-900 rounded p-3">
              <strong>{data.uncategorised_count} uncategorised transaction{data.uncategorised_count === 1 ? "" : "s"} excluded.</strong>{" "}
              Categorise them before relying on BAS turnover boxes.
            </div>
          )}
          <Card title={`${fyLabel(data.fy_year)} Q${data.quarter} · ${formatDate(data.period_start)} → ${formatDate(data.period_end)}`}>
            <div className="max-w-2xl">
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-600 bg-slate-50">
                <tr>
                  <th className="text-left px-3 py-2 w-16">Box</th>
                  <th className="text-left px-3 py-2">Label</th>
                  <th className="text-right px-3 py-2">Amount</th>
                </tr>
              </thead>
              <tbody>
                <BasRow
                  box="G1"
                  label="Total sales (gross IN on business accounts)"
                  value={data.g1_total_sales}
                />
                <BasRow
                  box="1A"
                  label="GST on sales"
                  value={data.one_a_gst_on_sales}
                />
                <BasRow
                  box="Purch."
                  label="Total purchase outflows (GST Exposure breaks out G10/G11/G14)"
                  value={data.total_purchases}
                />
                <BasRow
                  box="1B"
                  label="GST on purchases"
                  value={data.one_b_gst_on_purchases}
                />
                <tr className="bg-slate-50 font-semibold">
                  <td className="px-3 py-2"></td>
                  <td className="px-3 py-2">Net GST payable / (refund)</td>
                  <td className="px-3 py-2 text-right font-mono">
                    {formatMoney(data.net_gst_payable)}
                  </td>
                </tr>
              </tbody>
            </table>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

function BasRow({
  box,
  label,
  value,
}: {
  box: string;
  label: string;
  value: string;
}) {
  return (
    <tr className="border-b last:border-b-0 odd:bg-surface even:bg-slate-50">
      <td className="px-3 py-2 font-mono text-slate-500">{box}</td>
      <td className="px-3 py-2">{label}</td>
      <td className="px-3 py-2 text-right font-mono">{formatMoney(value)}</td>
    </tr>
  );
}


// ---------------------------------------------------------------------------
// Trial Balance tab (M2.2)
// ---------------------------------------------------------------------------

function TrialBalanceTab() {
  const currentId = useCompanyStore((s) => s.currentId);
  const [asOf, setAsOf] = useState<string>(toLocalIsoDate(new Date()));

  const { data, isLoading, error } = useQuery({
    queryKey: ["trial-balance", currentId, asOf],
    queryFn: async () => {
      const { data } = await api.get<TrialBalanceReport>(
        "/reports/trial-balance",
        { params: asOf ? { as_of: asOf } : {} },
      );
      return data;
    },
    enabled: !!currentId,
  });

  return (
    <div className="space-y-4">
      <Card
        title="Filter"
        right={
          data ? (
            <button
              className="btn-secondary text-xs"
              onClick={() =>
                downloadPdf(
                  "/reports/trial-balance/pdf",
                  asOf ? { as_of: asOf } : {},
                )
              }
            >
              Download PDF
            </button>
          ) : null
        }
      >
        <div className="flex items-end gap-3 text-sm">
          <label className="block">
            <span className="text-xs text-slate-500 block mb-0.5">As of</span>
            <DateInput value={asOf} onChange={setAsOf} className="border rounded px-2 py-1 pr-7" />
          </label>
          <button
            onClick={() => setAsOf("")}
            className="text-xs text-slate-600 hover:text-slate-900 underline ml-2 pb-1"
            title="Show all-time totals"
          >
            clear (all-time)
          </button>
        </div>
      </Card>

      {isLoading && (
        <Card title="Loading…">
          <div className="px-3 py-3 text-sm text-slate-500">Loading…</div>
        </Card>
      )}
      {error && (
        <Card title="Error">
          <div className="px-3 py-3 text-sm text-rose-700">
            {apiErrorMessage(error)}
          </div>
        </Card>
      )}

      {data && (
        <>
          <Card title="Status">
            <div className="flex flex-wrap gap-6 px-3 py-3 text-sm">
              <Kpi
                label="Total debits"
                value={formatMoney(data.total_debit)}
              />
              <Kpi
                label="Total credits"
                value={formatMoney(data.total_credit)}
              />
              <Kpi
                label="Difference"
                value={formatMoney(data.diff)}
                tone={data.is_balanced ? "ok" : "warn"}
              />
              <Kpi
                label="Status"
                value={data.is_balanced ? "Balanced ✓" : "Out of balance"}
                tone={data.is_balanced ? "ok" : "warn"}
              />
              {(Number(data.uncategorised_bank_in) > 0 ||
                Number(data.uncategorised_bank_out) > 0) && (
                <Kpi
                  label="Uncategorised txns"
                  value={`+${formatMoney(data.uncategorised_bank_in)} / -${formatMoney(
                    data.uncategorised_bank_out,
                  )}`}
                  tone="warn"
                />
              )}
            </div>
            {!data.is_balanced && (
              <div className="px-3 pb-3 text-xs text-amber-700">
                Categorise the uncategorised bank transactions on the
                Reconciliation page to bring this back into balance.
              </div>
            )}
          </Card>

          <Card title="Per-account totals">
            <table className="w-full text-sm">
              <thead className="text-left text-slate-500 border-b bg-slate-50">
                <tr>
                  <th className="py-2 px-3 w-20">Code</th>
                  <th className="py-2 px-3">Account / Bank</th>
                  <th className="py-2 px-3 w-28">Type</th>
                  <th className="py-2 px-3 w-32 text-right">Debit</th>
                  <th className="py-2 px-3 w-32 text-right">Credit</th>
                  <th className="py-2 px-3 w-32 text-right">Net Dr</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                      No postings yet.
                    </td>
                  </tr>
                )}
                {data.rows.map((r) => (
                  <tr key={r.key} className="border-b last:border-b-0">
                    <td className="py-1.5 px-3 font-mono text-xs">
                      {r.code ?? (r.kind === "bank" ? (
                        <span className="text-slate-400 not-italic">bank</span>
                      ) : (
                        "—"
                      ))}
                    </td>
                    <td className="py-1.5 px-3">{r.name}</td>
                    <td className="py-1.5 px-3 text-xs text-slate-500">
                      {r.kind === "bank" ? "Bank" : r.account_type ?? "—"}
                    </td>
                    <td className="py-1.5 px-3 text-right tabular-nums">
                      {Number(r.debit_total) > 0 ? formatMoney(r.debit_total) : ""}
                    </td>
                    <td className="py-1.5 px-3 text-right tabular-nums">
                      {Number(r.credit_total) > 0 ? formatMoney(r.credit_total) : ""}
                    </td>
                    <td
                      className={`py-1.5 px-3 text-right tabular-nums ${
                        Number(r.net_debit) < 0 ? "text-rose-700" : ""
                      }`}
                    >
                      {formatMoney(r.net_debit)}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="border-t bg-slate-50 font-semibold">
                <tr>
                  <td colSpan={3} className="py-2 px-3">Totals</td>
                  <td className="py-2 px-3 text-right tabular-nums">
                    {formatMoney(data.total_debit)}
                  </td>
                  <td className="py-2 px-3 text-right tabular-nums">
                    {formatMoney(data.total_credit)}
                  </td>
                  <td className="py-2 px-3 text-right tabular-nums">
                    {formatMoney(data.diff)}
                  </td>
                </tr>
              </tfoot>
            </table>
          </Card>

          <Card title="Supplementary (not included in main totals)">
            <div className="px-3 py-3 text-sm space-y-1">
              <div>
                Accounts Payable outstanding:{" "}
                <span className="font-mono">{formatMoney(data.supplementary.ap_open_total)}</span>
              </div>
              <div>
                Accounts Receivable outstanding:{" "}
                <span className="font-mono">{formatMoney(data.supplementary.ar_open_total)}</span>
              </div>
              <div className="text-xs text-slate-500 pt-2">
                These figures aren't in the main trial balance yet — see the
                Balance Sheet for a consolidated view.
              </div>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Balance Sheet tab (M2.2)
// ---------------------------------------------------------------------------

function BalanceSheetTab() {
  const currentId = useCompanyStore((s) => s.currentId);
  const [asOf, setAsOf] = useState<string>(toLocalIsoDate(new Date()));

  const { data, isLoading, error } = useQuery({
    queryKey: ["balance-sheet", currentId, asOf],
    queryFn: async () => {
      const { data } = await api.get<BalanceSheetReport>(
        "/reports/balance-sheet",
        { params: asOf ? { as_of: asOf } : {} },
      );
      return data;
    },
    enabled: !!currentId && !!asOf,
  });

  return (
    <div className="space-y-4">
      <Card
        title="Filter"
        right={
          data ? (
            <button
              className="btn-secondary text-xs"
              onClick={() =>
                downloadPdf(
                  "/reports/balance-sheet/pdf",
                  asOf ? { as_of: asOf } : {},
                )
              }
            >
              Download PDF
            </button>
          ) : null
        }
      >
        <div className="flex items-end gap-3 text-sm px-3 py-2">
          <label className="block">
            <span className="text-xs text-slate-500 block mb-0.5">As of</span>
            <DateInput value={asOf} onChange={setAsOf} className="border rounded px-2 py-1 pr-7" />
          </label>
        </div>
      </Card>

      {isLoading && (
        <Card title="Loading…">
          <div className="px-3 py-3 text-sm text-slate-500">Loading…</div>
        </Card>
      )}
      {error && (
        <Card title="Error">
          <div className="px-3 py-3 text-sm text-rose-700">
            {apiErrorMessage(error)}
          </div>
        </Card>
      )}

      {data && (
        <>
          <Card title="Balance check">
            <div className="flex flex-wrap gap-6 px-3 py-3 text-sm">
              <Kpi label="Assets" value={formatMoney(data.total_assets)} />
              <Kpi
                label="Liabilities + Equity"
                value={formatMoney(
                  (Number(data.total_liabilities) + Number(data.total_equity)).toFixed(2),
                )}
              />
              <Kpi
                label="Status"
                value={data.is_balanced ? "Balanced ✓" : `Diff ${formatMoney(data.diff)}`}
                tone={data.is_balanced ? "ok" : "warn"}
              />
            </div>
          </Card>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <BalanceSheetSide title="Assets" groups={data.assets} total={data.total_assets} />
            <div className="space-y-4">
              <BalanceSheetSide
                title="Liabilities"
                groups={data.liabilities}
                total={data.total_liabilities}
              />
              <BalanceSheetSide
                title="Equity"
                groups={data.equity}
                total={data.total_equity}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function BalanceSheetSide({
  title,
  groups,
  total,
}: {
  title: string;
  groups: BalanceSheetReport["assets"];
  total: string;
}) {
  return (
    <Card title={title}>
      <table className="w-full text-sm">
        <tbody>
          {groups.length === 0 && (
            <tr>
              <td colSpan={2} className="px-3 py-6 text-center text-slate-500">
                Nothing here yet.
              </td>
            </tr>
          )}
          {groups.map((g) => (
            <Fragment key={`${title}-${g.label}`}>
              <tr className="bg-slate-50 border-t">
                <td colSpan={2} className="px-3 py-1.5 text-xs uppercase tracking-wide text-slate-600">
                  {g.label}
                </td>
              </tr>
              {g.lines.map((l, i) => (
                <tr key={`${title}-${g.label}-${i}`} className="border-t">
                  <td className="py-1 px-3">
                    {l.code ? <span className="font-mono text-xs text-slate-500 mr-2">{l.code}</span> : null}
                    {l.name}
                  </td>
                  <td className="py-1 px-3 text-right tabular-nums">
                    {formatMoney(l.balance)}
                  </td>
                </tr>
              ))}
              <tr key={`${title}-${g.label}-sub`} className="border-t">
                <td className="py-1 px-3 text-xs text-slate-500 italic">
                  Subtotal — {g.label}
                </td>
                <td className="py-1 px-3 text-right tabular-nums font-medium">
                  {formatMoney(g.subtotal)}
                </td>
              </tr>
            </Fragment>
          ))}
        </tbody>
        <tfoot className="border-t bg-slate-50 font-semibold">
          <tr>
            <td className="py-2 px-3">Total {title}</td>
            <td className="py-2 px-3 text-right tabular-nums">
              {formatMoney(total)}
            </td>
          </tr>
        </tfoot>
      </table>
    </Card>
  );
}

function Kpi({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn" | "neutral";
}) {
  const toneClass =
    tone === "ok"
      ? "text-emerald-700"
      : tone === "warn"
        ? "text-amber-700"
        : "text-slate-900";
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`font-semibold tabular-nums ${toneClass}`}>{value}</div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// GST exposure tab (M2.3)
// ---------------------------------------------------------------------------

function GSTTab() {
  const currentId = useCompanyStore((s) => s.currentId);
  const today = new Date();
  const fyEndYear = today.getMonth() + 1 >= 7 ? today.getFullYear() + 1 : today.getFullYear();
  const currentQuarter = (() => {
    const m = today.getMonth() + 1;
    if (m >= 7 && m <= 9) return 1;
    if (m >= 10 && m <= 12) return 2;
    if (m >= 1 && m <= 3) return 3;
    return 4;
  })();

  const [fyYear, setFyYear] = useState<number>(fyEndYear);
  const [quarter, setQuarter] = useState<number>(currentQuarter);

  const { data, isLoading, error } = useQuery({
    queryKey: ["gst-exposure", currentId, fyYear, quarter],
    queryFn: async () => {
      const { data } = await api.get<GSTExposureReport>("/reports/gst-exposure", {
        params: { fy_year: fyYear, quarter },
      });
      return data;
    },
    enabled: !!currentId,
  });

  return (
    <div className="space-y-4">
      <Card
        title="Period"
        right={
          data ? (
            <button
              className="btn-secondary text-xs"
              onClick={() =>
                downloadPdf("/reports/gst-exposure/pdf", {
                  fy_year: fyYear,
                  quarter,
                })
              }
            >
              Download PDF
            </button>
          ) : null
        }
      >
        <div className="flex items-end gap-3 text-sm px-3 py-2">
          <label className="block">
            <span className="text-xs text-slate-500 block mb-0.5">Financial year</span>
            <select
              value={fyYear}
              onChange={(e) => setFyYear(Number(e.target.value))}
              className="border rounded px-2 py-1"
            >
              {fyOptions(fyYear).map((year) => (
                <option key={year} value={year}>
                  {fyLabel(year)}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-slate-500 block mb-0.5">Quarter</span>
            <select
              value={quarter}
              onChange={(e) => setQuarter(Number(e.target.value))}
              className="border rounded px-2 py-1"
            >
              <option value={1}>Q1 (Jul–Sep)</option>
              <option value={2}>Q2 (Oct–Dec)</option>
              <option value={3}>Q3 (Jan–Mar)</option>
              <option value={4}>Q4 (Apr–Jun)</option>
            </select>
          </label>
        </div>
      </Card>

      {isLoading && (
        <Card title="Loading…">
          <div className="px-3 py-3 text-sm text-slate-500">Loading…</div>
        </Card>
      )}
      {error && (
        <Card title="Error">
          <div className="px-3 py-3 text-sm text-rose-700">{apiErrorMessage(error)}</div>
        </Card>
      )}

      {data && (
        <>
          <Card title={`${fyLabel(data.fy_year ?? fyYear)} Q${data.quarter ?? quarter} · ${formatDate(data.period_start)} → ${formatDate(data.period_end)}`}>
            <div className="flex flex-wrap gap-6 px-3 py-3 text-sm">
              <Kpi label="GST on sales (1A)" value={formatMoney(data.one_a_gst_on_sales)} />
              <Kpi
                label="GST on purchases (1B)"
                value={formatMoney(data.one_b_gst_on_purchases)}
              />
              <Kpi
                label="Net GST payable"
                value={formatMoney(data.net_gst_payable)}
                tone={Number(data.net_gst_payable) > 0 ? "warn" : "ok"}
              />
              {data.excluded_count > 0 && (
                <Kpi
                  label="Excluded txns (tax_code=none)"
                  value={String(data.excluded_count)}
                />
              )}
              {data.uncategorised_count > 0 && (
                <Kpi
                  label="Uncategorised txns excluded"
                  value={String(data.uncategorised_count)}
                  tone="warn"
                />
              )}
            </div>
          </Card>

          {data.uncategorised_count > 0 && (
            <div className="text-sm bg-amber-50 border border-amber-200 text-amber-900 rounded p-3">
              <strong>{data.uncategorised_count} uncategorised transaction{data.uncategorised_count === 1 ? "" : "s"} excluded.</strong>{" "}
              Categorise them before relying on GST exposure boxes.
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Card title="Sales">
              <table className="w-full max-w-2xl text-sm">
                <tbody>
                  <GstBox box="G1" label="Total sales (gross)" value={data.g1_total_sales} />
                  <GstBox box="G3" label="GST-free sales" value={data.g3_gst_free_sales} />
                  <GstBox
                    box="G4"
                    label="Input-taxed sales"
                    value={data.g4_input_taxed_sales}
                  />
                  <GstBox
                    box="G6"
                    label="Sales subject to GST (G1−G3−G4)"
                    value={data.g6_sales_subject_to_gst}
                    bold
                  />
                  <GstBox
                    box="1A"
                    label="GST collected"
                    value={data.one_a_gst_on_sales}
                    bold
                  />
                </tbody>
              </table>
            </Card>

            <Card title="Purchases">
              <table className="w-full max-w-2xl text-sm">
                <tbody>
                  <GstBox
                    box="G10"
                    label="Capital purchases"
                    value={data.g10_capital_purchases}
                  />
                  <GstBox
                    box="G11"
                    label="Non-capital purchases"
                    value={data.g11_non_capital_purchases}
                  />
                  <GstBox
                    box="G14"
                    label="GST-free purchases"
                    value={data.g14_gst_free_purchases}
                  />
                  <GstBox
                    box="1B"
                    label="GST claimable"
                    value={data.one_b_gst_on_purchases}
                    bold
                  />
                </tbody>
              </table>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function GstBox({
  box,
  label,
  value,
  bold,
}: {
  box: string;
  label: string;
  value: string;
  bold?: boolean;
}) {
  return (
    <tr className={`border-t ${bold ? "bg-slate-50" : ""}`}>
      <td className="py-1.5 px-3 font-mono text-xs text-slate-500 w-14">{box}</td>
      <td className={`py-1.5 px-3 ${bold ? "font-medium" : ""}`}>{label}</td>
      <td
        className={`py-1.5 px-3 text-right tabular-nums ${bold ? "font-semibold" : ""}`}
      >
        {formatMoney(value)}
      </td>
    </tr>
  );
}
