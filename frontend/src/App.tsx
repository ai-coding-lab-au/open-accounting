import { Route, Routes } from "react-router-dom";
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

export default function App() {
  // Subscribe to the privacy toggle at the very top so a flip re-renders the
  // whole tree — format helpers (formatMoney / displayName / ...) read the
  // store via getState() and don't subscribe themselves.
  const privacyOn = usePrivacyEnabled();
  return (
    <div className="h-full flex flex-col md:flex-row overflow-hidden">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0">
        <TopBar />
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
      </div>
    </div>
  );
}
