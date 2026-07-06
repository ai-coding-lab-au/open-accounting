import axios from "axios";
import { useCompanyStore } from "../store/company";

export const api = axios.create({
  baseURL: "/api/v1",
  timeout: 15000,
});

api.interceptors.request.use((config) => {
  const currentId = useCompanyStore.getState().currentId;
  if (currentId) {
    config.headers["X-Company-Id"] = currentId;
  }
  return config;
});
