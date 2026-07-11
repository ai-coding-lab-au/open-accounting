import { useState } from "react";
import { Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import Sidebar from "./components/layout/Sidebar";
import TopBar from "./components/layout/TopBar";
import AccountingPage from "./pages/Accounting";
import Dashboard from "./pages/Dashboard";
import HomePage from "./pages/Home";
import InvoicesPage from "./pages/Invoices";
import DocumentsPage from "./pages/Documents";
import ContactsHomePage from "./pages/ContactsHome";
import ClientsPage from "./pages/Clients";
import BankAccountPage from "./pages/BusinessAccount";
import ReconciliationPage from "./pages/Reconciliation";
import ReportsPage from "./pages/Reports";
import AccountsPage from "./pages/Accounts";
import BankRulesPage from "./pages/BankRules";
import JournalPage from "./pages/Journal";
import ProvidersPage from "./pages/Providers";
import SettingsPage from "./pages/Settings";
import { usePrivacyEnabled } from "./lib/usePrivacy";
import { useCompanyStore } from "./store/company";
import { createLocalQueryClient } from "./lib/queryClient";

function CompanyWorkspace({ privacyOn }: { privacyOn: boolean }) {
  // Company queries fail in place instead of retrying after this workspace is
  // abandoned. A fresh client is created on every company-identity transition.
  const [queryClient] = useState(() => createLocalQueryClient({ queryRetry: 0 }));

  return (
    <QueryClientProvider client={queryClient}>
      <main key={privacyOn ? "p1" : "p0"} className="flex-1 min-w-0 overflow-auto p-4 md:p-6">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/accounting" element={<AccountingPage />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/journal" element={<JournalPage />} />
          <Route path="/bank-rules" element={<BankRulesPage />} />
          <Route path="/contacts" element={<ProvidersPage />} />
          <Route path="/invoices" element={<InvoicesPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/contacts-hub" element={<ContactsHomePage />} />
          <Route path="/clients" element={<ClientsPage />} />
          <Route path="/business-account" element={<BankAccountPage />} />
          <Route path="/reconciliation" element={<ReconciliationPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </QueryClientProvider>
  );
}

export default function App() {
  // Subscribe to the privacy toggle at the very top so a flip re-renders the
  // whole tree — format helpers (formatMoney / displayName / ...) read the
  // store via getState() and don't subscribe themselves.
  const privacyOn = usePrivacyEnabled();
  const currentCompanyId = useCompanyStore((s) => s.currentId);
  const currentCompanyGeneration = useCompanyStore(
    (s) => s.currentGeneration,
  );
  const currentCompanyKey = JSON.stringify([
    currentCompanyId,
    currentCompanyGeneration,
  ]);
  const companyIdentityPending =
    (currentCompanyId === null) !== (currentCompanyGeneration === null);
  return (
    <div className="h-full flex flex-col md:flex-row overflow-hidden">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar />
        {/* Company ID and database generation jointly own this subtree. A new
            instance is created for A1 -> B -> A1 and A1 -> recreated A2. Old
            query/mutation caches are never reused, while TopBar keeps the
            stable global client needed for company create/delete. */}
        {companyIdentityPending ? (
          <main className="flex-1 min-w-0 overflow-auto p-4 md:p-6">
            <p role="status" className="text-sm text-slate-500">
              Loading company…
            </p>
          </main>
        ) : (
          <CompanyWorkspace
            key={currentCompanyKey}
            privacyOn={privacyOn}
          />
        )}
      </div>
    </div>
  );
}
