import axios from "axios";
import { useCompanyStore } from "../store/company";

export const api = axios.create({
  baseURL: "/api/v1",
  timeout: 15000,
});

api.interceptors.request.use((config) => {
  const { currentId, currentGeneration } = useCompanyStore.getState();

  // A caller may explicitly bind a request to the company identity that owns
  // the UI object being acted on. Never overwrite an explicit id/generation
  // with a later selection: cancelling is safer than applying an old object's
  // numeric id to a different company or a recreated database generation.
  const explicitId = config.headers.get("X-Company-Id");
  if (explicitId != null && String(explicitId) !== currentId) {
    throw new axios.CanceledError("Company changed before request was sent");
  }

  const explicitGeneration = config.headers.get("X-Company-Generation");
  if (
    explicitGeneration != null &&
    String(explicitGeneration) !== currentGeneration
  ) {
    throw new axios.CanceledError(
      "Company database changed before request was sent",
    );
  }

  if (currentId && explicitId == null) {
    config.headers.set("X-Company-Id", currentId);
  }
  if (currentId && currentGeneration && explicitGeneration == null) {
    config.headers.set("X-Company-Generation", currentGeneration);
  }
  return config;
}, (error) => {
  // Axios's synchronous chain calls the rejection handler directly. Rethrow
  // so a deliberate CanceledError reaches the caller and dispatch never runs.
  throw error;
}, { synchronous: true });
