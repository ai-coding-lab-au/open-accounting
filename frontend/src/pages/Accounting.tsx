import { Link } from "react-router-dom";

const groups = [
  {
    title: "Ledger",
    items: [
      { to: "/dashboard", label: "Dashboard" },
      { to: "/accounts", label: "Chart of Accounts" },
      { to: "/journal", label: "Journal Entries" },
    ],
  },
  {
    title: "Banking",
    items: [
      { to: "/business-account", label: "Bank Account" },
      { to: "/bank-rules", label: "Bank Rules" },
      { to: "/reconciliation", label: "Reconciliation" },
    ],
  },
  {
    title: "Reporting",
    items: [{ to: "/reports", label: "Reports" }],
  },
];

export default function AccountingPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Accounting</h1>
      </div>

      <div className="grid gap-5 lg:grid-cols-3">
        {groups.map((group) => (
          <section key={group.title} className="space-y-2">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              {group.title}
            </h2>
            <div className="grid gap-2">
              {group.items.map((item) => (
                <Link
                  key={item.to}
                  to={item.to}
                  className="block rounded-md border border-slate-200 bg-surface px-4 py-3 text-sm font-medium text-slate-800 hover:border-emerald-300 hover:bg-slate-50"
                >
                  {item.label}
                </Link>
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
