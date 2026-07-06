import { NavLink, useLocation } from "react-router-dom";

type Item = {
  to: string;
  label: string;
  // Extra paths that should ALSO highlight this nav item (sub-pages
  // reachable from the hub but not in the sidebar themselves).
  matchPrefixes?: string[];
};

const items: Item[] = [
  { to: "/", label: "Home" },
  {
    to: "/accounting",
    label: "Accounting",
    matchPrefixes: [
      "/dashboard",
      "/accounts",
      "/journal",
      "/bank-rules",
      "/reconciliation",
      "/reports",
      "/business-account",
    ],
  },
  {
    to: "/documents",
    label: "Documents",
    matchPrefixes: ["/invoices"],
  },
  {
    to: "/contacts-hub",
    label: "Contacts",
    matchPrefixes: ["/clients", "/contacts"],
  },
  { to: "/settings", label: "Settings" },
];

function isItemActive(item: Item, pathname: string): boolean {
  if (item.to === "/") return pathname === "/";
  if (pathname === item.to || pathname.startsWith(item.to + "/")) return true;
  if (item.matchPrefixes) {
    return item.matchPrefixes.some(
      (p) => pathname === p || pathname.startsWith(p + "/"),
    );
  }
  return false;
}

export default function Sidebar() {
  const { pathname } = useLocation();
  return (
    <aside
      className="sidebar-shell shrink-0 flex flex-row md:flex-col w-full md:w-[200px] md:min-w-[200px]"
    >
      <NavLink
        to="/"
        className="px-4 py-3 text-sm font-semibold tracking-wide border-r md:border-r-0 md:border-b border-slate-700 hover:bg-slate-800 whitespace-nowrap"
        style={{ fontFamily: "system-ui, -apple-system, sans-serif" }}
      >
        Open Accounting
      </NavLink>
      <nav className="flex-1 flex md:block overflow-x-auto md:overflow-x-visible md:overflow-y-auto md:py-2">
        {items.map((item) => {
          const active = isItemActive(item, pathname);
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={`sidebar-nav-link block px-3 md:px-4 py-3 md:py-2 text-sm whitespace-nowrap border-b-4 md:border-b-0 md:border-l-4 ${
                active
                  ? "is-active border-emerald-400 font-semibold"
                  : "border-transparent"
              }`}
            >
              {item.label}
            </NavLink>
          );
        })}
      </nav>
    </aside>
  );
}
