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

type DesktopModelProfile = {
  id: string; name: string; protocol: "compatible" | "stepfun";
  baseUrl: string; model: string; jsonMode: boolean; hasKey: boolean;
};
type DesktopModelStatus = {
  enabled: boolean; hasKey: boolean; model: string; issue?: string | null;
  activeProfileId: string; profiles: DesktopModelProfile[];
};

interface Window {
  vistoraDesktop?: {
    platform: string;
    apiRequest: <T = unknown>(input: DesktopApiRequest) => Promise<DesktopApiResponse<T>>;
    appVersion: () => Promise<string>;
    localStatus: () => Promise<DesktopModelStatus | null>;
    localMaintenance: (input: { operation: "ai" | "backup" | "restore"; enabled?: boolean; consent?: boolean; key?: string; model?: string; profileId?: string; deleteProfileId?: string; name?: string; protocol?: string; baseUrl?: string; jsonMode?: boolean; password?: string; clientState?: Record<string, string> }) => Promise<{
      ok: boolean; error?: string; canceled?: boolean;
      status?: DesktopModelStatus;
      clientState?: Record<string, string>;
    }>;
    window: {
      minimize: () => void;
      toggleMaximize: () => void;
      close: () => void;
    };
  };
}
