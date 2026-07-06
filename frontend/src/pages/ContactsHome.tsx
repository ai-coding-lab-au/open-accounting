import { Link } from "react-router-dom";

const items = [
  { to: "/clients", label: "Clients" },
  { to: "/contacts", label: "Providers" },
];

export default function ContactsHomePage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Contacts</h1>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className="block rounded-md border border-slate-200 bg-surface px-4 py-3 text-sm font-medium text-slate-800 hover:border-emerald-300 hover:bg-slate-50"
          >
            {item.label}
          </Link>
        ))}
      </div>
    </div>
  );
}
