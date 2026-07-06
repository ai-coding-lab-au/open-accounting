import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { apiErrorMessage } from "../../lib/errors";
import { useModalKeys } from "../../lib/useModalKeys";
import type {
  ImportExcelResult,
  InvoiceDirection,
  SpreadsheetPreview,
} from "../../types/api";

async function uploadExcel(file: File): Promise<SpreadsheetPreview> {
  const fd = new FormData();
  fd.append("file", file);
  const { data } = await api.post<SpreadsheetPreview>("/invoices/upload-excel", fd, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 60_000,
  });
  return data;
}

async function importRows(payload: {
  mapping: Record<string, number | null>;
  rows: SpreadsheetPreview["rows"];
  direction_default: InvoiceDirection;
}): Promise<ImportExcelResult> {
  const { data } = await api.post<ImportExcelResult>("/invoices/import-excel-rows", payload, {
    timeout: 120_000,
  });
  return data;
}

interface Props {
  onClose: () => void;
}

export default function ImportExcelDialog({ onClose }: Props) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SpreadsheetPreview | null>(null);
  const [mapping, setMapping] = useState<Record<string, number | null>>({});
  const [direction, setDirection] = useState<InvoiceDirection>("AP");
  const [result, setResult] = useState<ImportExcelResult | null>(null);

  const uploadMut = useMutation({
    mutationFn: uploadExcel,
    onSuccess: (res) => {
      setPreview(res);
      setMapping(res.mapping);
    },
  });

  const importMut = useMutation({
    mutationFn: importRows,
    onSuccess: (res) => {
      setResult(res);
      qc.invalidateQueries({ queryKey: ["invoices"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  const setField = (field: string, idx: number | null) =>
    setMapping((m) => ({ ...m, [field]: idx }));

  const REQUIRED = ["contact_name", "invoice_number", "issue_date", "total"];
  const missing = REQUIRED.filter((f) => mapping[f] == null);
  const canUpload = !preview && !result && !!file && !uploadMut.isPending;
  const canImport = !!preview && !result && missing.length === 0 && !importMut.isPending;

  useModalKeys({
    open: true,
    onClose,
    onSubmit: () => {
      if (canUpload && file) uploadMut.mutate(file);
      else if (canImport && preview) {
        importMut.mutate({
          mapping,
          rows: preview.rows,
          direction_default: direction,
        });
      }
    },
  });

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-surface rounded-lg shadow-xl w-[920px] max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-slate-200 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Import invoices from Excel / CSV</h2>
          <button className="text-slate-500 hover:text-slate-900" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="px-5 py-4 overflow-auto">
          {!preview && !result && (
            <>
              <p className="text-sm text-slate-600 mb-3">
                Pick an <code>.xlsx</code> or <code>.csv</code>. Row 1 must be headers; we'll
                propose a column mapping that you can override.
              </p>
              <input
                type="file"
                accept=".xlsx,.xlsm,.csv"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
              {file && (
                <p className="text-xs text-slate-500 mt-2">
                  {file.name} · {(file.size / 1024).toFixed(1)} KB
                </p>
              )}
              {uploadMut.isError && (
                <p className="text-sm text-red-600 mt-3">{apiErrorMessage(uploadMut.error)}</p>
              )}
            </>
          )}

          {preview && !result && (
            <>
              <div className="mb-4">
                <h3 className="text-sm font-medium mb-2">
                  Column mapping{" "}
                  <span className="text-xs text-slate-500">
                    ({preview.headers.length} columns, {preview.rows.length} rows)
                  </span>
                </h3>
                <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                  {preview.field_options.map((field) => (
                    <label key={field} className="flex items-center gap-2">
                      <span className="w-32 text-slate-600">{field}</span>
                      <select
                        className="input flex-1"
                        value={mapping[field] ?? ""}
                        onChange={(e) =>
                          setField(field, e.target.value === "" ? null : Number(e.target.value))
                        }
                      >
                        <option value="">— ignore —</option>
                        {preview.headers.map((h, i) => (
                          <option key={i} value={i}>
                            col {i + 1}: {h || "(blank)"}
                          </option>
                        ))}
                      </select>
                    </label>
                  ))}
                </div>
                {missing.length > 0 && (
                  <p className="text-xs text-amber-700 mt-2">
                    Required fields without a column: {missing.join(", ")}
                  </p>
                )}
              </div>

              <div className="mb-3 flex items-center gap-3 text-sm">
                <span className="text-slate-600">Default direction when row has none:</span>
                <select
                  className="input w-32"
                  value={direction}
                  onChange={(e) => setDirection(e.target.value as InvoiceDirection)}
                >
                  <option value="AP">AP (bills)</option>
                  <option value="AR">AR (sales)</option>
                </select>
              </div>

              <h3 className="text-sm font-medium mb-2">
                Preview <span className="text-xs text-slate-500">(first 8 rows)</span>
              </h3>
              <div className="overflow-auto border border-slate-200 rounded">
                <table className="text-xs min-w-full">
                  <thead className="bg-slate-50 text-slate-600">
                    <tr>
                      <th className="px-2 py-1 text-left">#</th>
                      {preview.headers.map((h, i) => (
                        <th key={i} className="px-2 py-1 text-left whitespace-nowrap">
                          {h || `col ${i + 1}`}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.slice(0, 8).map((r) => (
                      <tr key={r.row_no} className="border-t border-slate-100">
                        <td className="px-2 py-1 text-slate-500">{r.row_no}</td>
                        {r.cells.map((c, i) => (
                          <td key={i} className="px-2 py-1 whitespace-nowrap">
                            {c}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {importMut.isError && (
                <p className="text-sm text-red-600 mt-3">{apiErrorMessage(importMut.error)}</p>
              )}
            </>
          )}

          {result && (
            <div className="space-y-3 text-sm">
              <p>
                <span className="font-semibold text-emerald-700">{result.created.length}</span>{" "}
                invoice(s) created.
              </p>
              {result.skipped.length > 0 ? (
                <div>
                  <p className="text-amber-700">{result.skipped.length} row(s) skipped:</p>
                  <ul className="list-disc list-inside mt-1 text-slate-700">
                    {result.skipped.map((s, i) => (
                      <li key={i}>
                        row {s.row}: {s.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p className="text-slate-600">No rows skipped.</p>
              )}
            </div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-200 flex justify-end gap-2">
          {!preview && !result && (
            <>
              <button className="btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                className="btn-primary"
                disabled={!canUpload}
                onClick={() => file && uploadMut.mutate(file)}
              >
                {uploadMut.isPending ? "Parsing…" : "Next: review mapping"}
              </button>
            </>
          )}
          {preview && !result && (
            <>
              <button
                className="btn-secondary"
                onClick={() => {
                  setPreview(null);
                  setFile(null);
                }}
                disabled={importMut.isPending}
              >
                Back
              </button>
              <button
                className="btn-primary"
                disabled={!canImport}
                onClick={() =>
                  importMut.mutate({
                    mapping,
                    rows: preview.rows,
                    direction_default: direction,
                  })
                }
              >
                {importMut.isPending
                  ? "Importing…"
                  : `Import ${preview.rows.length} row(s)`}
              </button>
            </>
          )}
          {result && (
            <button className="btn-primary" onClick={onClose}>
              Done
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
