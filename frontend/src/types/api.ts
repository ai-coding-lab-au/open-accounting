export interface Company {
  id: string;
  generation_id: string;
  name: string;
  legal_name: string | null;
  abn: string | null;
  country: string;
  base_currency: string;
  fy_start_month: number;
  gst_registered: boolean;
  bilingual_labels: boolean;
  address_line1: string | null;
  address_line2: string | null;
  suburb: string | null;
  state: string | null;
  postcode: string | null;
  phone: string | null;
  email: string | null;
  website: string | null;
  bank_account_name: string | null;
  bank_name: string | null;
  bank_bsb: string | null;
  bank_account_number: string | null;
  bank_swift: string | null;
  operating_bank_account_name: string | null;
  operating_bank_name: string | null;
  operating_bank_bsb: string | null;
  operating_bank_account_number: string | null;
  operating_bank_swift: string | null;
  default_payment_terms_days: number;
  books_locked_through: string | null;
  acn: string | null;
  created_at: string;
}

export type CompanyUpdate = Partial<
  Omit<Company, "id" | "generation_id" | "country" | "base_currency" | "created_at">
>;

export interface CompanyCreate {
  id: string;
  name: string;
  legal_name?: string | null;
  abn?: string | null;
  base_currency?: string;
  fy_start_month?: number;
  gst_registered?: boolean;
  default_payment_terms_days?: number;
}

export type AccountType =
  | "ASSET"
  | "LIABILITY"
  | "EQUITY"
  | "INCOME"
  | "EXPENSE"
  | "COST_OF_SALES";

export interface Account {
  id: number;
  code: string;
  name: string;
  type: string;
  parent_id: number | null;
  is_gst: boolean;
  active: boolean;
  description: string | null;
}

export interface AccountCreate {
  code: string;
  name: string;
  type: AccountType;
  parent_id?: number | null;
  is_gst?: boolean;
  description?: string | null;
}

export interface AccountUpdate {
  code?: string;
  name?: string;
  type?: AccountType;
  parent_id?: number | null;
  set_parent_null?: boolean;
  is_gst?: boolean;
  active?: boolean;
  description?: string | null;
}

export interface Contact {
  id: number;
  name: string;
  kind: "customer" | "supplier" | "both";
  abn: string | null;
  email: string | null;
  phone: string | null;
  address: string | null;
  notes: string | null;
  active: boolean;
  created_at?: string;
}

export interface ContactCreate {
  name: string;
  kind: "customer" | "supplier" | "both";
  abn?: string | null;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  notes?: string | null;
  active?: boolean;
}

export interface ContactUpdate {
  name?: string;
  kind?: "customer" | "supplier" | "both";
  abn?: string | null;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  notes?: string | null;
  active?: boolean;
}

export type InvoiceDirection = "AP" | "AR";
export type InvoiceStatus =
  | "draft"
  | "authorised"
  | "unpaid"
  | "partial"
  | "paid"
  | "void";
export type InvoiceSource = "manual" | "pdf" | "excel";

export interface AttachmentOut {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  uploaded_at: string;
}

export interface Invoice {
  id: number;
  direction: InvoiceDirection;
  contact_id: number;
  contact_name: string | null;
  invoice_number: string;
  issue_date: string;
  due_date: string | null;
  currency: string;
  subtotal: string;
  gst_amount: string;
  total: string;
  gst_inclusive: boolean;
  status: InvoiceStatus;
  paid_amount: string;
  paid_date: string | null;
  notes: string | null;
  source: InvoiceSource;
  source_ref: string | null;
  created_at: string;
  updated_at: string;
  attachments: AttachmentOut[];
}

export interface InvoiceCreate {
  direction: InvoiceDirection;
  contact_id?: number | null;
  contact_name?: string | null;
  contact_abn?: string | null;
  invoice_number: string;
  issue_date: string;
  due_date?: string | null;
  currency?: string;
  subtotal: string;
  gst_amount?: string;
  total: string;
  gst_inclusive?: boolean;
  notes?: string | null;
  source?: InvoiceSource;
  source_ref?: string | null;
  attachment_id?: string | null;
  lines?: InvoiceLineIn[] | null;
}

export interface InvoiceLineIn {
  description: string;
  account_id?: number | null;
  line_subtotal: string;
  line_gst?: string;
  line_total: string;
  tax_code?: TaxCode;
}

export interface InvoiceUpdate {
  direction?: InvoiceDirection;
  issue_date?: string | null;
  due_date?: string | null;
  contact_id?: number | null;
  invoice_number?: string;
  currency?: string;
  subtotal?: string;
  gst_amount?: string;
  total?: string;
  gst_inclusive?: boolean;
  status?: InvoiceStatus;
  paid_amount?: string;
  paid_date?: string | null;
  notes?: string | null;
  lines?: InvoiceLineIn[] | null;
}

export interface PdfUploadResult {
  attachment_id: string;
  filename: string;
  size_bytes: number;
}

export interface SpreadsheetPreviewRow {
  row_no: number;
  cells: string[];
  raw: (string | number | null)[];
}

export interface SpreadsheetPreview {
  headers: string[];
  mapping: Record<string, number | null>;
  rows: SpreadsheetPreviewRow[];
  field_options: string[];
}

export interface ImportExcelResult {
  created: number[];
  skipped: { row: number; reason: string }[];
}

export type OutgoingDocType = "receipt";
export type OutgoingDocStatus = "draft" | "issued" | "void";

export interface OutgoingLine {
  id?: number;
  order_no?: number;
  description: string;
  quantity: string;
  unit_price: string;
  amount?: string;
}

export interface OutgoingDocument {
  id: number;
  doc_type: OutgoingDocType;
  doc_number: string;
  issue_date: string;
  customer_id: number | null;
  client_ref_id: number | null;
  customer_name: string;
  customer_address: string | null;
  customer_abn?: string | null;
  customer_email: string | null;
  customer_phone: string | null;
  currency: string;
  subtotal: string;
  gst_amount: string;
  total: string;
  status: OutgoingDocStatus;
  paid_date: string | null;
  payment_method: string | null;
  notes: string | null;
  pdf_rel_path: string | null;
  created_at: string;
  updated_at: string;
  lines: Required<OutgoingLine>[];
}

export interface OutgoingCreate {
  doc_type: OutgoingDocType;
  issue_date: string;
  customer_id?: number | null;
  client_ref_id?: number | null;
  customer_name?: string | null;
  customer_address?: string | null;
  customer_email?: string | null;
  customer_phone?: string | null;
  currency?: string;
  lines: { description: string; quantity: string; unit_price: string; amount?: string }[];
  notes?: string | null;
  payment_method?: string | null;
  paid_date?: string | null;
  doc_number_override?: string | null;
}

export interface DocCounter {
  doc_type: OutgoingDocType;
  year: number;
  last_number: number;
  next_preview: string;
}

// ---------------------------------------------------------------------------
// Clients — who we serve; Contacts are providers we pay (different table).
// ---------------------------------------------------------------------------

export interface Client {
  id: number;
  display_name: string;
  email: string | null;
  phone: string | null;
  address: string | null;
  client_ref: string | null;
  notes: string | null;
  is_active: boolean;
  created_at: string;
}

export interface ClientCreate {
  display_name: string;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  client_ref?: string | null;
  notes?: string | null;
}

export type ClientUpdate = Partial<ClientCreate> & { is_active?: boolean };

export interface BankAccount {
  id: number;
  name: string;
  bsb: string | null;
  account_number: string | null;
  opening_balance: string;
  is_active: boolean;
  notes: string | null;
  created_at: string;
}

export interface BankAccountWithBalance extends BankAccount {
  current_balance: string;
}

export type BankTxnDirection = "in" | "out";

export type TaxCode = "standard" | "gst_free" | "input_taxed" | "capital" | "none";

export interface InvoicePaymentAllocationIn {
  invoice_id: number;
  amount: string;
}

export interface InvoicePaymentTaxComponent {
  tax_code: TaxCode;
  gross_amount: string;
  gst_amount: string;
}

export interface InvoicePaymentAllocation extends InvoicePaymentAllocationIn {
  id: number;
  gst_amount: string;
  tax_components: InvoicePaymentTaxComponent[];
}

export interface BankTransaction {
  id: number;
  bank_account_id: number;
  direction: BankTxnDirection;
  amount: string;
  occurred_at: string;
  memo: string | null;
  counter_party_name: string | null;
  account_id: number | null;
  gst_amount: string;
  tax_code: TaxCode;
  created_at: string;
  invoice_allocations: InvoicePaymentAllocation[];
  unapplied_account_id: number | null;
  unapplied_amount: string;
}

export interface BankTransactionIn {
  direction: BankTxnDirection;
  amount: string;
  occurred_at: string;
  memo?: string | null;
  counter_party_name?: string | null;
  account_id?: number | null;
  gst_amount?: string;
  tax_code?: TaxCode;
  invoice_allocations?: InvoicePaymentAllocationIn[];
  unapplied_account_id?: number | null;
}

export interface BankTransactionUpdate {
  memo?: string | null;
  counter_party_name?: string | null;
  account_id?: number | null;
  set_account_null?: boolean;
  gst_amount?: string | null;
  tax_code?: TaxCode | null;
  invoice_allocations?: InvoicePaymentAllocationIn[] | null;
  unapplied_account_id?: number | null;
}

// ---------------------------------------------------------------------------
// Reports (M3)
// ---------------------------------------------------------------------------

export interface BankStatementRow {
  id: number;
  occurred_at: string;
  direction: BankTxnDirection;
  amount: string;
  gst_amount: string;
  memo: string | null;
  counter_party_name: string | null;
  account_code: string | null;
  account_name: string | null;
  running_balance: string;
}

export interface BankStatement {
  bank_account_id: number;
  bank_account_name: string;
  year: number;
  month: number;
  period_start: string;
  period_end: string;
  opening_balance: string;
  closing_balance: string;
  total_in: string;
  total_out: string;
  net_change: string;
  rows: BankStatementRow[];
}


export interface PnLLine {
  account_id: number;
  code: string;
  name: string;
  total: string;
}

export interface PnLReport {
  period_start: string;
  period_end: string;
  income_rows: PnLLine[];
  cogs_rows: PnLLine[];
  expense_rows: PnLLine[];
  uncategorised_in: string;
  uncategorised_out: string;
  total_income: string;
  total_cogs: string;
  total_expense: string;
  gross_profit: string;
  net_profit: string;
}

export interface BASReport {
  fy_year: number;
  quarter: number;
  period_start: string;
  period_end: string;
  g1_total_sales: string;
  one_a_gst_on_sales: string;
  total_purchases: string;
  one_b_gst_on_purchases: string;
  net_gst_payable: string;
  uncategorised_count: number;
  gst_registered: boolean;
}

// ---------------------------------------------------------------------------
// Bank rules + import (M3)
// ---------------------------------------------------------------------------

export interface BankRule {
  id: number;
  priority: number;
  is_active: boolean;
  description: string;
  match_direction: "in" | "out" | null;
  match_amount_min: string | null;
  match_amount_max: string | null;
  match_memo_regex: string | null;
  match_counter_party_regex: string | null;
  set_account_id: number;
  set_tax_code: TaxCode;
  created_at: string;
}

export interface BankRuleCreate {
  priority?: number;
  is_active?: boolean;
  description: string;
  match_direction?: "in" | "out" | null;
  match_amount_min?: string | null;
  match_amount_max?: string | null;
  match_memo_regex?: string | null;
  match_counter_party_regex?: string | null;
  set_account_id: number;
  set_tax_code?: TaxCode;
}

export type BankRuleUpdate = Partial<BankRuleCreate>;


export interface BankImportRowParsed {
  occurred_at: string | null;
  memo: string | null;
  counter_party_name: string | null;
  direction: "in" | "out" | null;
  amount: string | null;
}

export interface BankImportPreviewRow {
  row_no: number;
  cells: string[];
  parsed: BankImportRowParsed;
  ok: boolean;
  issue: string | null;
  dedup_key: string | null;
  is_duplicate: boolean | null;
  suggested_account_id: number | null;
  suggested_tax_code: TaxCode | null;
  suggested_gst_amount: string | null;
  suggestion_source: "rule" | "heuristic" | null;
  matched_rule_id: number | null;
  matched_rule_description: string | null;
}

export interface BankImportPreview {
  bank_account_id: number;
  headers: string[];
  mapping: Record<string, number | null>;
  field_options: string[];
  rows: BankImportPreviewRow[];
}

export interface BankImportCommitRow {
  occurred_at: string;
  direction: "in" | "out";
  amount: string;
  dedup_key?: string | null;
  account_id?: number | null;
  tax_code?: TaxCode;
  memo?: string | null;
  counter_party_name?: string | null;
  gst_amount?: string;
  invoice_allocations?: InvoicePaymentAllocationIn[];
  unapplied_account_id?: number | null;
}

export interface BankImportCommitResult {
  created: number;
  skipped_duplicates: number;
}


export interface GSTExposureReport {
  period_start: string;
  period_end: string;
  fy_year: number | null;
  quarter: number | null;
  gst_registered: boolean;

  g1_total_sales: string;
  g3_gst_free_sales: string;
  g4_input_taxed_sales: string;
  g6_sales_subject_to_gst: string;
  one_a_gst_on_sales: string;

  g10_capital_purchases: string;
  g11_non_capital_purchases: string;
  g14_gst_free_purchases: string;
  one_b_gst_on_purchases: string;

  net_gst_payable: string;
  excluded_count: number;
  uncategorised_count: number;
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

export interface DashBankAccount {
  id: number;
  name: string;
  balance: string;
}

export interface DashTxnRow {
  id: number;
  occurred_at: string;
  direction: string;
  amount: string;
  memo: string | null;
  counter_party_name: string | null;
  account_code: string | null;
  account_name: string | null;
}

export interface DashApRow {
  id: number;
  invoice_number: string;
  contact_name: string | null;
  issue_date: string;
  due_date: string | null;
  total: string;
  outstanding: string;
  is_overdue: boolean;
}

export interface DashboardSummary {
  as_of: string;
  fy_year: number;
  fy_period: { start: string; end: string };
  current_month: { start: string; end: string };
  current_quarter: { fy_year: number; quarter: number; start: string; end: string };

  bank_accounts: DashBankAccount[];
  business_total: string;
  unpaid_ap_total: string;
  overdue_ap_count: number;

  fy_net_profit: string;
  fy_total_income: string;
  fy_total_expense: string;

  month_income: string;
  month_expense: string;
  month_uncategorised_in: string;
  month_uncategorised_out: string;

  tb_balanced: boolean;
  tb_diff: string;
  tb_uncategorised_in: string;
  tb_uncategorised_out: string;

  recent_business_txns: DashTxnRow[];
  unpaid_ap: DashApRow[];
}


// ---------------------------------------------------------------------------
// Manual journal entries (M2.1)
// ---------------------------------------------------------------------------

export interface JournalLine {
  id: number;
  account_id: number;
  debit_amount: string;   // decimal as string from backend
  credit_amount: string;
  description: string | null;
}

export interface JournalEntry {
  id: number;
  entry_date: string;     // YYYY-MM-DD
  memo: string;
  reference: string | null;
  created_at: string;
  updated_at: string;
  lines: JournalLine[];
}

export interface JournalLineCreate {
  account_id: number;
  debit_amount?: string;
  credit_amount?: string;
  description?: string | null;
}

export interface JournalEntryCreate {
  entry_date: string;
  memo: string;
  reference?: string | null;
  lines: JournalLineCreate[];
}

export interface JournalEntryUpdate {
  entry_date?: string;
  memo?: string;
  reference?: string | null;
  lines?: JournalLineCreate[];
}


// ---------------------------------------------------------------------------
// Trial Balance + Balance Sheet (M2.2)
// ---------------------------------------------------------------------------

export interface TrialBalanceRow {
  key: string;
  kind: "account" | "bank";
  ref_id: number;
  code: string | null;
  name: string;
  account_type: string | null;
  debit_total: string;
  credit_total: string;
  net_debit: string;
}

export interface TrialBalanceSupplementary {
  ap_open_total: string;
  ar_open_total: string;
}

export interface TrialBalanceReport {
  as_of: string | null;
  rows: TrialBalanceRow[];
  total_debit: string;
  total_credit: string;
  diff: string;
  is_balanced: boolean;
  uncategorised_bank_in: string;
  uncategorised_bank_out: string;
  supplementary: TrialBalanceSupplementary;
}

export interface BalanceSheetLine {
  account_id: number | null;
  code: string | null;
  name: string;
  balance: string;
}

export interface BalanceSheetGroup {
  label: string;
  lines: BalanceSheetLine[];
  subtotal: string;
}

export interface BalanceSheetReport {
  as_of: string;
  assets: BalanceSheetGroup[];
  liabilities: BalanceSheetGroup[];
  equity: BalanceSheetGroup[];
  total_assets: string;
  total_liabilities: string;
  total_equity: string;
  is_balanced: boolean;
  diff: string;
}

// ---------------------------------------------------------------------------
// Staff (document signers)
// ---------------------------------------------------------------------------

export type StaffRegistrationType = "mara" | "lpn" | "none";

export interface StaffMember {
  id: number;
  full_name: string;
  registration_type: StaffRegistrationType;
  registration_number: string | null;
  active: boolean;
  display_label: string;
}

export interface StaffUpsert {
  full_name: string;
  registration_type: StaffRegistrationType;
  registration_number: string | null;
  active: boolean;
}
