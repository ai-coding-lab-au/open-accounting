import { Link } from "react-router-dom";

const modules = [
  {
    to: "/accounting",
    label: "Accounting",
    meta: "Ledger / Banking / Reporting",
  },
  {
    to: "/documents",
    label: "Documents",
    meta: "Supplier invoices / Receipts",
  },
  {
    to: "/contacts-hub",
    label: "Contacts",
    meta: "Clients / Providers",
  },
  {
    to: "/settings",
    label: "Settings",
    meta: "Company / Privacy / Numbering",
  },
];

export default function HomePage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Open Accounting</h1>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        {modules.map((module) => (
          <Link
            key={module.to}
            to={module.to}
            className="block rounded-md border border-slate-200 bg-surface px-5 py-4 hover:border-emerald-300 hover:bg-slate-50"
          >
            <div className="text-base font-semibold text-slate-900">{module.label}</div>
            <div className="mt-2 text-xs text-slate-500">{module.meta}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
