/**
 * Shared e2e helpers.
 *
 * Spec files run in alphabetical order against a single freshly-wiped
 * backend (see e2e/run-e2e.sh). Tests that need a company / client to
 * exist before driving the UI should create them via the API here
 * rather than depending on the UI of a sibling spec.
 */

import type { APIRequestContext } from "@playwright/test";

export const COMPANY_ID = "e2eco";
export const COMPANY_NAME = "E2E Test Pty Ltd";

export const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://127.0.0.1:8765";

export interface CompanyIdentity {
  id: string;
  generation_id: string;
}

const companyGenerations = new Map<string, string>();

export function companyHeaders(companyId: string): Record<string, string> {
  const generation = companyGenerations.get(companyId);
  if (!generation) {
    throw new Error(
      `No generation cached for company ${companyId}; call getCompanyById or ensureCompanyById first`,
    );
  }
  return {
    "X-Company-Id": companyId,
    "X-Company-Generation": generation,
  };
}

export async function getCompanyById(
  request: APIRequestContext,
  id: string,
): Promise<CompanyIdentity | null> {
  const list = await request.get(`${BACKEND_URL}/api/v1/companies`);
  if (!list.ok()) throw new Error(`GET /companies failed: ${list.status()}`);
  const companies = (await list.json()) as CompanyIdentity[];
  const company = companies.find((candidate) => candidate.id === id) ?? null;
  if (company) companyGenerations.set(company.id, company.generation_id);
  else companyGenerations.delete(id);
  return company;
}

export async function ensureCompanyById(
  request: APIRequestContext,
  id: string,
  name: string,
): Promise<CompanyIdentity> {
  const existing = await getCompanyById(request, id);
  if (existing) return existing;

  const created = await request.post(`${BACKEND_URL}/api/v1/companies`, {
    data: {
      id,
      name,
      abn: null,
      gst_registered: true,
    },
  });
  if (!created.ok()) {
    throw new Error(
      `POST /companies failed: ${created.status()} ${await created.text()}`,
    );
  }
  const company = (await created.json()) as CompanyIdentity;
  companyGenerations.set(company.id, company.generation_id);
  return company;
}

export async function ensureCompany(
  request: APIRequestContext,
): Promise<void> {
  await ensureCompanyById(request, COMPANY_ID, COMPANY_NAME);

  // Keep legacy company signer fields populated for older templates.
  // Current SA creation is gated on an active registered staff member;
  // use ensureSignerStaff for that.
  const patched = await request.patch(
    `${BACKEND_URL}/api/v1/companies/${COMPANY_ID}`,
    {
      headers: companyHeaders(COMPANY_ID),
      data: {
        marn: "1234567",
        registered_agent_name: "Test Agent",
      },
    },
  );
  if (!patched.ok()) {
    throw new Error(
      `PATCH /companies/${COMPANY_ID} failed: ${patched.status()} ${await patched.text()}`,
    );
  }
}

export async function ensureClient(
  request: APIRequestContext,
  display_name: string,
): Promise<number> {
  // List existing first (idempotent across runs).
  const list = await request.get(`${BACKEND_URL}/api/v1/clients`, {
    headers: companyHeaders(COMPANY_ID),
  });
  if (list.ok()) {
    const clients = (await list.json()) as Array<{ id: number; display_name: string }>;
    const found = clients.find((c) => c.display_name === display_name);
    if (found) return found.id;
  }

  const created = await request.post(`${BACKEND_URL}/api/v1/clients`, {
    headers: companyHeaders(COMPANY_ID),
    data: { display_name },
  });
  if (!created.ok()) {
    throw new Error(
      `POST /clients failed: ${created.status()} ${await created.text()}`,
    );
  }
  const body = (await created.json()) as { id: number };
  return body.id;
}

export async function ensureSignerStaff(
  request: APIRequestContext,
): Promise<number> {
  const headers = companyHeaders(COMPANY_ID);
  const list = await request.get(
    `${BACKEND_URL}/api/v1/staff?include_inactive=true`,
    { headers },
  );
  if (!list.ok()) throw new Error(`GET /staff failed: ${list.status()}`);

  const staff = (await list.json()) as Array<{
    id: number;
    full_name: string;
    registration_type: "mara" | "lpn" | "none";
    registration_number: string | null;
    active: boolean;
  }>;
  const activeSigner = staff.find(
    (row) =>
      row.active &&
      (row.registration_type === "mara" || row.registration_type === "lpn"),
  );
  if (activeSigner) return activeSigner.id;

  const reusable = staff.find((row) => row.full_name === "E2E Signer");
  if (reusable) {
    const updated = await request.put(
      `${BACKEND_URL}/api/v1/staff/${reusable.id}`,
      {
        headers,
        data: {
          full_name: "E2E Signer",
          registration_type: "lpn",
          registration_number: "12345",
          active: true,
        },
      },
    );
    if (!updated.ok()) {
      throw new Error(
        `PUT /staff/${reusable.id} failed: ${updated.status()} ${await updated.text()}`,
      );
    }
    return ((await updated.json()) as { id: number }).id;
  }

  const created = await request.post(`${BACKEND_URL}/api/v1/staff`, {
    headers,
    data: {
      full_name: "E2E Signer",
      registration_type: "lpn",
      registration_number: "12345",
    },
  });
  if (!created.ok()) {
    throw new Error(
      `POST /staff failed: ${created.status()} ${await created.text()}`,
    );
  }
  return ((await created.json()) as { id: number }).id;
}
