/// <reference types="vite/client" />

type DesktopApiRequest = {
  baseUrl: string;
  path: string;
  method?: string;
  timeoutMs?: number;
  token?: string;
  vaultId?: string;
  headers?: Record<string, string>;
  body?: unknown;
};

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_DEV_AUTH_TOKEN?: string;
  readonly VITE_VAULT_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

type DesktopApiResponse<T = unknown> = {
  ok: boolean;
  status: number;
  data: T;
  headers: { etag?: string | null; contentType?: string | null };
};

interface Window {
  vistoraDesktop?: {
    platform: string;
    apiRequest: <T = unknown>(input: DesktopApiRequest) => Promise<DesktopApiResponse<T>>;
    appVersion: () => Promise<string>;
    window: {
      minimize: () => void;
      toggleMaximize: () => void;
      close: () => void;
    };
  };
}
