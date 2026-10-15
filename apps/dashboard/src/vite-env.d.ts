/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional absolute API origin, e.g. "http://127.0.0.1:8000". */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
