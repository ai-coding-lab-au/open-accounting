import { QueryClient } from "@tanstack/react-query";

export function createLocalQueryClient({
  queryRetry = 1,
}: {
  queryRetry?: number;
} = {}): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The API is local. Losing internet connectivity must not defer a
        // request until a later company selection is active.
        networkMode: "always",
        refetchOnReconnect: true,
        refetchOnWindowFocus: false,
        retry: queryRetry,
      },
      mutations: {
        // Execute immediately against the local API. Never retain an offline
        // mutation that could first run after a company switch.
        networkMode: "always",
        retry: 0,
      },
    },
  });
}
