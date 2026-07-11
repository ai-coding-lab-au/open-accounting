import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { formatDate, formatMoney } from "../../lib/format";
import { blockScientificNotation } from "../../lib/numericInput";
import { todayLocal } from "../../lib/date";
import { useCompanyStore } from "../../store/company";
import type {
  Account,
  BankTxnDirection,
  Invoice,
  InvoiceDirection,
  InvoicePaymentAllocationIn,
} from "../../types/api";

type ControlRequirement = {
  invoiceDirection: InvoiceDirection;
  compatible: boolean;
  accountLabel: string;
  movementLabel: string;
};

export function isInvoiceControlAccount(account: Account | null | undefined): boolean {
  return account?.code === "1100" || account?.code === "2000";
}

export function invoiceControlRequirement(
  account: Account | null | undefined,
  direction: BankTxnDirection,
): ControlRequirement | null {
  if (account?.code === "1100") {
    return {
      invoiceDirection: "AR",
      compatible: direction === "in",
      accountLabel: "Accounts Receivable (1100)",
      movementLabel: "customer receipt",
    };
  }
  if (account?.code === "2000") {
    return {
      invoiceDirection: "AP",
      compatible: direction === "out",
      accountLabel: "Accounts Payable (2000)",
      movementLabel: "supplier payment",
    };
  }
  return null;
}

function moneyToCents(value: string): bigint | null {
  const match = value.trim().match(/^(\d+)(?:\.(\d{0,2}))?$/);
  if (!match) return null;
  try {
    const fraction = (match[2] ?? "").padEnd(2, "0");
    return BigInt(match[1]) * 100n + BigInt(fraction || "0");
  } catch {
    return null;
  }
}

function centsToMoney(value: bigint): string {
  return `${value / 100n}.${(value % 100n).toString().padStart(2, "0")}`;
}

function invoiceOutstandingCents(invoice: Invoice): bigint {
  const total = moneyToCents(invoice.total) ?? 0n;
  const paid = moneyToCents(invoice.paid_amount) ?? 0n;
  return total > paid ? total - paid : 0n;
}

function allocationsTotalCents(
  allocations: InvoicePaymentAllocationIn[],
): bigint | null {
  let total = 0n;
  for (const allocation of allocations) {
    const cents = moneyToCents(allocation.amount);
    if (cents === null) return null;
    total += cents;
  }
  return total;
}

export function allocationsEqualTransactionAmount(
  allocations: InvoicePaymentAllocationIn[],
  transactionAmount: string,
): boolean {
  const target = moneyToCents(transactionAmount);
  const allocated = allocationsTotalCents(allocations);
  return (
    target !== null &&
    target > 0n &&
    allocated !== null &&
    allocations.length > 0 &&
    new Set(allocations.map((row) => row.invoice_id)).size === allocations.length &&
    allocations.every((row) => (moneyToCents(row.amount) ?? 0n) > 0n) &&
    allocated === target
  );
}

export function allocationsDoNotExceedTransactionAmount(
  allocations: InvoicePaymentAllocationIn[],
  transactionAmount: string,
): boolean {
  const target = moneyToCents(transactionAmount);
  const allocated = allocationsTotalCents(allocations);
  return (
    target !== null &&
    target > 0n &&
    allocated !== null &&
    allocated > 0n &&
    allocated <= target &&
    new Set(allocations.map((row) => row.invoice_id)).size === allocations.length &&
    allocations.every((row) => (moneyToCents(row.amount) ?? 0n) > 0n)
  );
}

async function fetchInvoices(
  direction: InvoiceDirection,
  transactionDate: string,
): Promise<Invoice[]> {
  const { data } = await api.get<Invoice[]>("/invoices", {
    params: { direction, to: transactionDate },
  });
  return data;
}

export function InvoiceAllocationEditor({
  account,
  direction,
  transactionDate,
  transactionAmount,
  accounts,
  allocations,
  onChange,
  unappliedAccountId,
  onUnappliedAccountChange,
  onValidityChange,
  disabled = false,
}: {
  account: Account | null | undefined;
  direction: BankTxnDirection;
  transactionDate: string;
  transactionAmount: string;
  accounts: Account[];
  allocations: InvoicePaymentAllocationIn[];
  onChange: (allocations: InvoicePaymentAllocationIn[]) => void;
  unappliedAccountId: number | "";
  onUnappliedAccountChange: (accountId: number | "") => void;
  onValidityChange: (valid: boolean) => void;
  disabled?: boolean;
}) {
  const currentId = useCompanyStore((state) => state.currentId);
  const requirement = invoiceControlRequirement(account, direction);
  const targetCents = moneyToCents(transactionAmount);
  const futureDated = transactionDate > todayLocal();

  const invoicesQ = useQuery({
    queryKey: [
      "invoice-payment-candidates",
      currentId,
      requirement?.invoiceDirection,
      transactionDate,
    ],
    queryFn: () => fetchInvoices(requirement!.invoiceDirection, transactionDate),
    enabled:
      !!currentId &&
      !!requirement?.compatible &&
      !futureDated &&
      transactionDate.trim() !== "",
    // Outstanding balances change whenever another settlement is saved. Never
    // trust a cached candidate list when a fresh allocation editor opens.
    refetchOnMount: "always",
  });

  const eligibleInvoices = useMemo(
    () =>
      (invoicesQ.data ?? [])
        .filter(
          (invoice) =>
            invoice.direction === requirement?.invoiceDirection &&
            ["authorised", "unpaid", "partial"].includes(invoice.status) &&
            invoice.issue_date <= transactionDate &&
            invoiceOutstandingCents(invoice) > 0n,
        )
        .sort(
          (left, right) =>
            left.issue_date.localeCompare(right.issue_date) || left.id - right.id,
        ),
    [invoicesQ.data, requirement?.invoiceDirection, transactionDate],
  );

  const invoicesById = useMemo(
    () => new Map(eligibleInvoices.map((invoice) => [invoice.id, invoice])),
    [eligibleInvoices],
  );
  const allocatedCents = allocationsTotalCents(allocations);
  const remainingCents =
    targetCents !== null && allocatedCents !== null ? targetCents - allocatedCents : null;
  const residualAccounts = useMemo(() => {
    const requiredType = requirement?.invoiceDirection === "AR" ? "LIABILITY" : "ASSET";
    const controlCodes = new Set(["1100", "1200", "2000", "2100"]);
    return accounts
      .filter(
        (candidate) =>
          candidate.active &&
          candidate.type === requiredType &&
          !controlCodes.has(candidate.code),
      )
      .sort((left, right) => {
        const preferredCode = requirement?.invoiceDirection === "AR" ? "2050" : "1500";
        if (left.code === preferredCode) return -1;
        if (right.code === preferredCode) return 1;
        return left.code.localeCompare(right.code);
      });
  }, [accounts, requirement?.invoiceDirection]);
  const residualAccountValid = residualAccounts.some(
    (candidate) => candidate.id === unappliedAccountId,
  );
  const allocationsWithinOutstanding = allocations.every((allocation) => {
    const invoice = invoicesById.get(allocation.invoice_id);
    const amountCents = moneyToCents(allocation.amount);
    return (
      !!invoice &&
      amountCents !== null &&
      amountCents > 0n &&
      amountCents <= invoiceOutstandingCents(invoice)
    );
  });
  const valid =
    !!requirement?.compatible &&
    !futureDated &&
    !invoicesQ.isLoading &&
    !invoicesQ.isError &&
    allocationsWithinOutstanding &&
    allocationsDoNotExceedTransactionAmount(allocations, transactionAmount) &&
    (remainingCents === 0n
      ? unappliedAccountId === ""
      : remainingCents !== null && remainingCents > 0n && residualAccountValid);

  useEffect(() => onValidityChange(valid), [onValidityChange, valid]);
  useEffect(() => {
    if (remainingCents === 0n) {
      if (unappliedAccountId !== "") onUnappliedAccountChange("");
      return;
    }
    if (remainingCents !== null && remainingCents > 0n && !residualAccountValid) {
      onUnappliedAccountChange(residualAccounts[0]?.id ?? "");
    }
  }, [
    onUnappliedAccountChange,
    remainingCents,
    residualAccountValid,
    residualAccounts,
    unappliedAccountId,
  ]);

  if (!requirement) return null;

  if (!requirement.compatible) {
    return (
      <div className="rounded border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">
        {requirement.accountLabel} cannot be used for this bank direction. It only
        accepts a {requirement.movementLabel}.
      </div>
    );
  }

  if (futureDated) {
    return (
      <div className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
        Future-dated bank transactions are scheduled, not settled. Keep this
        transaction uncategorised and allocate it on or after {formatDate(transactionDate)}.
      </div>
    );
  }

  const setInvoiceAmount = (invoiceId: number, amount: string) => {
    const existing = allocations.some((row) => row.invoice_id === invoiceId);
    onChange(
      existing
        ? allocations.map((row) =>
            row.invoice_id === invoiceId ? { ...row, amount } : row,
          )
        : [...allocations, { invoice_id: invoiceId, amount }],
    );
  };

  const toggleInvoice = (invoice: Invoice, checked: boolean) => {
    if (!checked) {
      onChange(allocations.filter((row) => row.invoice_id !== invoice.id));
      return;
    }
    const current = allocatedCents ?? 0n;
    const remaining = targetCents !== null && targetCents > current ? targetCents - current : 0n;
    const outstanding = invoiceOutstandingCents(invoice);
    const suggested = remaining > 0n && remaining < outstanding ? remaining : outstanding;
    setInvoiceAmount(invoice.id, centsToMoney(suggested));
  };

  return (
    <div
      className="rounded border border-blue-200 bg-blue-50/60 p-3 space-y-2"
      data-testid="invoice-allocation-editor"
    >
      <div>
        <p className="text-sm font-medium text-blue-950">Allocate this {requirement.movementLabel}</p>
        <p className="text-xs text-blue-800 mt-0.5">
          Select posted {requirement.invoiceDirection} invoices dated on or before the bank
          date. Invoice allocations may be less than, but cannot exceed,{" "}
          {formatMoney(transactionAmount || "0")}.
        </p>
      </div>

      {targetCents === null || targetCents <= 0n ? (
        <p className="text-xs text-amber-700">Enter a valid transaction amount first.</p>
      ) : invoicesQ.isLoading ? (
        <p className="text-xs text-slate-500">Loading open invoices...</p>
      ) : invoicesQ.isError ? (
        <p className="text-xs text-rose-700">Could not load invoices. Try again before saving.</p>
      ) : eligibleInvoices.length === 0 ? (
        <p className="text-xs text-amber-700">
          No eligible open {requirement.invoiceDirection} invoices exist for this date. Use
          a customer-deposit or supplier-prepayment account as the transaction category
          instead of the invoice control account.
        </p>
      ) : (
        <div className="max-h-52 overflow-auto rounded border border-blue-100 bg-white">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-slate-50 text-left text-slate-500">
              <tr>
                <th className="px-2 py-1 w-7"></th>
                <th className="px-2 py-1">Invoice</th>
                <th className="px-2 py-1">Date</th>
                <th className="px-2 py-1 text-right">Outstanding</th>
                <th className="px-2 py-1 w-28 text-right">Allocate</th>
              </tr>
            </thead>
            <tbody>
              {eligibleInvoices.map((invoice) => {
                const allocation = allocations.find((row) => row.invoice_id === invoice.id);
                const allocationCents = allocation ? moneyToCents(allocation.amount) : null;
                const outstanding = invoiceOutstandingCents(invoice);
                const overOutstanding = allocationCents !== null && allocationCents > outstanding;
                return (
                  <tr key={invoice.id} className="border-t border-slate-100">
                    <td className="px-2 py-1">
                      <input
                        type="checkbox"
                        aria-label={`Allocate invoice ${invoice.invoice_number}`}
                        checked={!!allocation}
                        disabled={disabled}
                        onChange={(event) => toggleInvoice(invoice, event.target.checked)}
                      />
                    </td>
                    <td className="px-2 py-1">
                      <span className="font-medium">{invoice.invoice_number}</span>
                      {invoice.contact_name && (
                        <span className="block text-[10px] text-slate-500">{invoice.contact_name}</span>
                      )}
                    </td>
                    <td className="px-2 py-1 font-mono">{formatDate(invoice.issue_date)}</td>
                    <td className="px-2 py-1 text-right tabular-nums">
                      {formatMoney(centsToMoney(outstanding))}
                    </td>
                    <td className="px-2 py-1 text-right">
                      <input
                        type="number"
                        step="0.01"
                        min="0.01"
                        max={centsToMoney(outstanding)}
                        className={`border rounded px-1 py-0.5 w-24 text-right ${
                          overOutstanding ? "border-rose-500" : "border-slate-300"
                        }`}
                        aria-label={`Amount for invoice ${invoice.invoice_number}`}
                        value={allocation?.amount ?? ""}
                        disabled={disabled || !allocation}
                        onChange={(event) => setInvoiceAmount(invoice.id, event.target.value)}
                        onKeyDown={blockScientificNotation}
                      />
                      {overOutstanding && (
                        <span className="block text-[10px] text-rose-700">Exceeds outstanding</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex justify-end gap-4 text-xs tabular-nums">
        <span>
          Allocated: <strong>{formatMoney(centsToMoney(allocatedCents ?? 0n))}</strong>
        </span>
        <span className={remainingCents === 0n ? "text-emerald-700" : "text-amber-700"}>
          {remainingCents === null
            ? "Enter an amount"
            : remainingCents === 0n
              ? "Fully allocated"
              : remainingCents > 0n
                ? `${formatMoney(centsToMoney(remainingCents))} unapplied`
                : `${formatMoney(centsToMoney(-remainingCents))} over`}
        </span>
      </div>

      {remainingCents !== null && remainingCents > 0n && (
        <div className="rounded border border-amber-200 bg-amber-50 p-3 space-y-2">
          <label className="block text-xs">
            <span className="block font-medium text-amber-950 mb-1">
              Account for {formatMoney(centsToMoney(remainingCents))} unapplied remainder
            </span>
            <select
              className="input text-sm"
              aria-label="Unapplied remainder account"
              value={unappliedAccountId === "" ? "" : String(unappliedAccountId)}
              disabled={disabled}
              onChange={(event) =>
                onUnappliedAccountChange(
                  event.target.value === "" ? "" : Number(event.target.value),
                )
              }
            >
              <option value="">- select an account -</option>
              {residualAccounts.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.code} - {candidate.name}
                </option>
              ))}
            </select>
          </label>
          {residualAccounts.length === 0 ? (
            <p className="text-xs text-rose-700">
              Create an active non-control{" "}
              {requirement.invoiceDirection === "AR" ? "Liability" : "Asset"} account
              before saving this partial allocation.
            </p>
          ) : (
            <p className="text-xs text-amber-800">
              The remainder is kept outside invoice settlement and GST allocation until
              it is applied through a later documented workflow.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
