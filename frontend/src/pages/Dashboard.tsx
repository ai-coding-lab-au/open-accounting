import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useCompanyStore } from "../store/company";
import { displayName, formatDate, formatMoney } from "../lib/format";
import type { DashboardSummary } from "../types/api";

async function fetchSummary(): Promise<DashboardSummary> {
  const { data } = await api.get<DashboardSummary>("/dashboard/summary");
  return data;
}

export default function Dashboard() {
  const currentId = useCompanyStore((s) => s.currentId);

  const { data, isLoading, error } = useQuery({
    queryKey: ["dashboard", currentId],
    queryFn: fetchSummary,
    enabled: !!currentId,
  });

  if (!currentId) {
    return <Placeholder title="No company selected" body="Pick a company in the top bar." />;
  }
  if (isLoading) {
    return (
      <div className="text-sm text-slate-500">Loading dashboard…</div>
    );
  }
  if (error || !data) {
    return (
      <div className="bg-rose-50 border border-rose-200 text-rose-800 rounded p-4 text-sm">
        {(error as Error)?.message ?? "Failed to load dashboard."}
      </div>
    );
  }

  const fyStart = formatDate(data.fy_period.start);
  const fyEnd = formatDate(data.fy_period.end);
  const fyDisplay = `FY ${data.fy_year - 1}-${String(data.fy_year).slice(2)}`;
  const monthStart = formatDate(data.current_month.start);
  const monthEnd = formatDate(data.current_month.end);

  const netProfitN = Number(data.fy_net_profit);
  const monthIncomeN = Number(data.month_income);
  const monthExpenseN = Number(data.month_expense);
  const monthMax = Math.max(monthIncomeN, monthExpenseN, 1);

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between flex-wrap gap-2">
        <div>
          <h1 className="text-xl font-semibold">Dashboard</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            As of {formatDate(data.as_of)} · {fyDisplay}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link
            to="/business-account"
            className="px-3 py-1.5 text-sm rounded border bg-surface hover:bg-slate-50"
          >
            Bank account →
          </Link>
          <Link
            to="/reports"
            className="px-3 py-1.5 text-sm rounded bg-slate-900 text-white hover:bg-slate-800"
          >
            Reports →
          </Link>
        </div>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          title="Bank balance"
          value={formatMoney(data.business_total)}
          sub={`${data.bank_accounts.length} account(s)`}
          accent="emerald"
        />
        <KpiCard
          title="Unpaid AP"
          value={formatMoney(data.unpaid_ap_total)}
          sub={
            data.overdue_ap_count > 0
              ? `${data.overdue_ap_count} overdue`
              : "All on schedule"
          }
          accent={data.overdue_ap_count > 0 ? "amber" : "slate"}
        />
        <KpiCard
          title="FY net profit"
          value={formatMoney(data.fy_net_profit)}
          sub={`${fyStart} → ${fyEnd}`}
          accent={netProfitN >= 0 ? "emerald" : "rose"}
        />
      </div>

      {/* Books-balanced banner (trial balance health, M2.2) */}
      {!data.tb_balanced && (
        <div className="bg-amber-50 border border-amber-200 text-amber-900 rounded p-3 text-sm">
          <strong>Books are out of balance</strong> by{" "}
          {formatMoney(data.tb_diff)}.{" "}
          {(Number(data.tb_uncategorised_in) > 0 ||
            Number(data.tb_uncategorised_out) > 0) && (
            <span>
              {formatMoney(data.tb_uncategorised_in)} in /{" "}
              {formatMoney(data.tb_uncategorised_out)} out of bank
              transactions are uncategorised.{" "}
              <Link to="/reconciliation" className="underline">
                Categorise →
              </Link>
            </span>
          )}{" "}
          <Link to="/reports" className="underline">
            See trial balance →
          </Link>
        </div>
      )}


      {/* Bank accounts list + month bars */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card title="Bank accounts">
          {data.bank_accounts.length === 0 ? (
            <p className="text-sm text-slate-500">No bank accounts yet.</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {data.bank_accounts.map((b) => (
                  <tr key={b.id} className="border-b last:border-b-0">
                    <td className="py-1.5">
                      <Link to="/business-account" className="hover:underline">
                        {displayName(b.name, "company")}
                      </Link>
                    </td>
                    <td className="py-1.5 text-right font-mono">
                      {formatMoney(b.balance)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title={`This month · ${monthStart} → ${monthEnd}`}>
          <BarRow
            label="Income"
            value={monthIncomeN}
            max={monthMax}
            color="bg-emerald-500"
          />
          <BarRow
            label="Expense"
            value={monthExpenseN}
            max={monthMax}
            color="bg-rose-500"
          />
          <div className="mt-3 pt-3 border-t text-sm flex justify-between">
            <span className="text-slate-500">Net</span>
            <span
              className={`font-semibold ${
                monthIncomeN - monthExpenseN >= 0
                  ? "text-emerald-700"
                  : "text-rose-700"
              }`}
            >
              {formatMoney(monthIncomeN - monthExpenseN)}
            </span>
          </div>
          {(Number(data.month_uncategorised_in) > 0 ||
            Number(data.month_uncategorised_out) > 0) && (
            <p className="text-xs text-amber-700 mt-2">
              {formatMoney(data.month_uncategorised_in)} in /{" "}
              {formatMoney(data.month_uncategorised_out)} out are
              uncategorised this month.{" "}
              <Link to="/business-account" className="underline">
                Categorise →
              </Link>
            </p>
          )}
        </Card>
      </div>

      {/* Recent activity + AP */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card
          title="Recent business transactions"
          action={
            <Link to="/business-account" className="text-xs underline">
              View all →
            </Link>
          }
        >
          {data.recent_business_txns.length === 0 ? (
            <p className="text-sm text-slate-500">No transactions yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-slate-500 border-b">
                <tr>
                  <th className="py-1 pr-2">Date</th>
                  <th className="py-1 pr-2">Description</th>
                  <th className="py-1 pr-2">Category</th>
                  <th className="py-1 text-right">Amount</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_business_txns.map((t) => (
                  <tr key={t.id} className="border-b last:border-b-0">
                    <td className="py-1 pr-2 text-slate-500">
                      {formatDate(t.occurred_at)}
                    </td>
                    <td className="py-1 pr-2">
                      <div>{t.memo ?? displayName(t.counter_party_name, "provider")}</div>
                      {t.counter_party_name && t.memo && (
                        <div className="text-xs text-slate-500">
                          {displayName(t.counter_party_name, "provider")}
                        </div>
                      )}
                    </td>
                    <td className="py-1 pr-2 text-slate-500">
                      {t.account_code ? (
                        <span>
                          {t.account_code} {t.account_name}
                        </span>
                      ) : (
                        <span className="text-amber-700">Uncategorised</span>
                      )}
                    </td>
                    <td
                      className={`py-1 text-right font-mono ${
                        t.direction === "in"
                          ? "text-emerald-700"
                          : "text-slate-700"
                      }`}
                    >
                      {t.direction === "in" ? "+" : "−"}
                      {formatMoney(t.amount)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card
          title="Unpaid supplier invoices"
          action={
            <Link to="/invoices" className="text-xs underline">
              View all →
            </Link>
          }
        >
          {data.unpaid_ap.length === 0 ? (
            <p className="text-sm text-slate-500">No unpaid AP.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-slate-500 border-b">
                <tr>
                  <th className="py-1 pr-2">Invoice</th>
                  <th className="py-1 pr-2">Supplier</th>
                  <th className="py-1 pr-2">Due</th>
                  <th className="py-1 text-right">Outstanding</th>
                </tr>
              </thead>
              <tbody>
                {data.unpaid_ap.map((inv) => (
                  <tr key={inv.id} className="border-b last:border-b-0">
                    <td className="py-1 pr-2 font-mono">{inv.invoice_number}</td>
                    <td className="py-1 pr-2">{displayName(inv.contact_name, "provider")}</td>
                    <td
                      className={`py-1 pr-2 ${
                        inv.is_overdue ? "text-rose-700" : "text-slate-500"
                      }`}
                    >
                      {formatDate(inv.due_date)}
                      {inv.is_overdue && " (overdue)"}
                    </td>
                    <td className="py-1 text-right font-mono">
                      {formatMoney(inv.outstanding)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </div>
  );
}

function KpiCard({
  title,
  value,
  sub,
  accent,
}: {
  title: string;
  value: string;
  sub: string;
  accent: "emerald" | "sky" | "amber" | "rose" | "slate";
}) {
  const colorMap: Record<typeof accent, string> = {
    emerald: "text-emerald-700",
    sky: "text-sky-700",
    amber: "text-amber-700",
    rose: "text-rose-700",
    slate: "text-slate-700",
  };
  return (
    <div className="bg-surface rounded-lg border border-slate-200 p-4">
      <div className="text-xs text-slate-500 uppercase tracking-wide">
        {title}
      </div>
      <div className={`text-2xl font-semibold mt-1 ${colorMap[accent]}`}>
        {value}
      </div>
      <div className="text-xs text-slate-500 mt-1">{sub}</div>
    </div>
  );
}

function Card({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-surface rounded-lg border border-slate-200">
      <div className="px-4 py-2 border-b flex justify-between items-center bg-slate-50">
        <h3 className="font-medium text-sm">{title}</h3>
        {action}
      </div>
      <div className="p-4">{children}</div>
    </div>
  );
}

function BarRow({
  label,
  value,
  max,
  color,
}: {
  label: string;
  value: number;
  max: number;
  color: string;
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="mb-2">
      <div className="flex justify-between text-sm">
        <span className="text-slate-600">{label}</span>
        <span className="font-mono">{formatMoney(value)}</span>
      </div>
      <div className="h-2 bg-slate-100 rounded mt-1">
        <div
          className={`h-2 rounded ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function Placeholder({ title, body }: { title: string; body: string }) {
  return (
    <div className="bg-surface rounded-lg border border-slate-200 p-6 text-center">
      <h2 className="font-semibold">{title}</h2>
      <p className="text-sm text-slate-500 mt-1">{body}</p>
    </div>
  );
}
