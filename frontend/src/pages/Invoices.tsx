import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Invoice } from "../types/api";
import ImportPdfDialog from "../components/invoices/ImportPdfDialog";
import ImportExcelDialog from "../components/invoices/ImportExcelDialog";
import InvoiceDetailDrawer from "../components/invoices/InvoiceDetailDrawer";
import InvoicesContent from "../components/invoices/InvoicesContent";
import ManualCreateDialog from "../components/invoices/ManualCreateDialog";

export default function InvoicesPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Invoice | null>(null);
  const [showPdf, setShowPdf] = useState(false);
  const [showExcel, setShowExcel] = useState(false);
  const [showManual, setShowManual] = useState(false);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Invoices</h1>
      </div>

      <section className="rounded-md border border-slate-200 bg-surface p-4">
        <div className="mb-3">
          <h2 className="text-sm font-semibold text-slate-900">Create invoice</h2>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn-primary" onClick={() => setShowManual(true)}>
            + Manual
          </button>
          <button className="btn-secondary" onClick={() => setShowExcel(true)}>
            Import Excel/CSV
          </button>
          <button className="btn-secondary" onClick={() => setShowPdf(true)}>
            Attach PDF + Manual
          </button>
        </div>
      </section>

      <InvoicesContent
        onSelectInvoice={setSelected}
        onCreateManual={() => setShowManual(true)}
        onImportExcel={() => setShowExcel(true)}
        showCreateActions={false}
        title="Recent invoices"
      />

      {selected && (
        <InvoiceDetailDrawer
          invoice={selected}
          onClose={() => {
            setSelected(null);
            qc.invalidateQueries({ queryKey: ["invoices"] });
          }}
        />
      )}

      {showPdf && <ImportPdfDialog onClose={() => setShowPdf(false)} />}
      {showExcel && <ImportExcelDialog onClose={() => setShowExcel(false)} />}
      {showManual && <ManualCreateDialog onClose={() => setShowManual(false)} />}
    </div>
  );
}
