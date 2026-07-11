import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { useCompanyStore } from "../store/company";
import type { Company } from "../types/api";

export function useCurrentCompany() {
  const currentId = useCompanyStore((s) => s.currentId);
  const currentGeneration = useCompanyStore((s) => s.currentGeneration);

  return useQuery({
    queryKey: ["company", currentId, currentGeneration],
    queryFn: async () => {
      if (!currentId) throw new Error("No company selected");
      return (await api.get<Company>(`/companies/${encodeURIComponent(currentId)}`)).data;
    },
    enabled: !!currentId && !!currentGeneration,
  });
}
