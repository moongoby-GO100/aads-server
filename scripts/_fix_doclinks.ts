"use client";

type DocPathMapping = {
  hostPrefix: string;
  project: string;
  basePath: string;
};

const DOC_PATH_MAPPINGS: DocPathMapping[] = [
  { hostPrefix: "/root/aads/aads-server/docs", project: "AADS", basePath: "/app/docs" },
  { hostPrefix: "/root/aads/aads-server/reports", project: "AADS", basePath: "/app/reports" },
  { hostPrefix: "/root/aads/aads-server/app/static/docs", project: "AADS", basePath: "/app/app/static/docs" },
  { hostPrefix: "/root/aads/aads-server/app/static/reports", project: "AADS", basePath: "/app/app/static/reports" },
  { hostPrefix: "/root/aads/aads-server/app/static/preview", project: "AADS", basePath: "/app/app/static/preview" },
  { hostPrefix: "/root/aads/aads-server/app/static/gallery", project: "AADS", basePath: "/app/app/static/gallery" },
  { hostPrefix: "/root/aads/aads-docs/docs", project: "AADS", basePath: "/root/aads/aads-docs/docs" },
  { hostPrefix: "/root/aads/aads-docs/reports", project: "AADS", basePath: "/root/aads/aads-docs/reports" },
  { hostPrefix: "/root/aads/aads-dashboard/docs", project: "AADS", basePath: "/root/aads/aads-dashboard/docs" },
  { hostPrefix: "/root/aads/aads-dashboard/reports", project: "AADS", basePath: "/root/aads/aads-dashboard/reports" },
  { hostPrefix: "/root/aads/aads-dashboard/public/reports", project: "AADS", basePath: "/root/aads/aads-dashboard/public/reports" },
  { hostPrefix: "/root/aads/aads-dashboard/public/exports", project: "AADS", basePath: "/root/aads/aads-dashboard/public/exports" },
  { hostPrefix: "/root/aads/aads-core/docs", project: "AADS", basePath: "/root/aads/aads-core/docs" },
  { hostPrefix: "/root/aads/aads-core/reports", project: "AADS", basePath: "/root/aads/aads-core/reports" },
  { hostPrefix: "/root/kis-autotrade-v4/docs", project: "KIS", basePath: "/root/kis-autotrade-v4/docs" },
  { hostPrefix: "/root/kis-autotrade-v4/report", project: "GO100", basePath: "/root/kis-autotrade-v4/report" },
  { hostPrefix: "/root/kis-autotrade-v4/reports", project: "GO100", basePath: "/root/kis-autotrade-v4/reports" },
  { hostPrefix: "/root/kis-autotrade-v4/docs/go100", project: "GO100", basePath: "/root/kis-autotrade-v4/docs/go100" },
  { hostPrefix: "/root/kis-autotrade-v4/docs/technical", project: "GO100", basePath: "/root/kis-autotrade-v4/docs/technical" },
  { hostPrefix: "/data/shortflow/docs", project: "SF", basePath: "/data/shortflow/docs" },
  { hostPrefix: "/srv/newtalk-v2/docs", project: "NTV2", basePath: "/srv/newtalk-v2/docs" },
];

type RelativeMapping = {
  prefix: string;
  project: string;
  basePath: string;
  stripPrefix: string;
};

const RELATIVE_DOC_MAPPINGS: RelativeMapping[] = [
  { prefix: "/app/app/static/docs/", project: "AADS", basePath: "/app/app/static/docs", stripPrefix: "/app/app/static/docs/" },
  { prefix: "/app/app/static/reports/", project: "AADS", basePath: "/app/app/static/reports", stripPrefix: "/app/app/static/reports/" },
  { prefix: "/app/app/static/preview/", project: "AADS", basePath: "/app/app/static/preview", stripPrefix: "/app/app/static/preview/" },
  { prefix: "/app/app/static/gallery/", project: "AADS", basePath: "/app/app/static/gallery", stripPrefix: "/app/app/static/gallery/" },
  { prefix: "/app/docs/", project: "AADS", basePath: "/app/docs", stripPrefix: "/app/docs/" },
  { prefix: "/app/reports/", project: "AADS", basePath: "/app/reports", stripPrefix: "/app/reports/" },
  { prefix: "app/static/docs/", project: "AADS", basePath: "/app/app/static/docs", stripPrefix: "app/static/docs/" },
  { prefix: "app/static/reports/", project: "AADS", basePath: "/app/app/static/reports", stripPrefix: "app/static/reports/" },
  { prefix: "app/static/preview/", project: "AADS", basePath: "/app/app/static/preview", stripPrefix: "app/static/preview/" },
  { prefix: "app/static/gallery/", project: "AADS", basePath: "/app/app/static/gallery", stripPrefix: "app/static/gallery/" },
  { prefix: "docs/", project: "AADS", basePath: "/app/docs", stripPrefix: "docs/" },
  { prefix: "reports/", project: "AADS", basePath: "/app/reports", stripPrefix: "reports/" },
];

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function buildDocsHref(project: string, basePath: string, filePath: string, line?: string, hash?: string): string {
  const q = new URLSearchParams();
  q.set("project", project);
  q.set("base_path", basePath);
  q.set("file_path", filePath.replace(/^\/+/, ""));
  if (line) q.set("line", line);
  return `/docs?${q.toString()}${hash || ""}`;
}

function splitPathSuffix(path: string): { filePath: string; line?: string; hash?: string } {
  const hashIndex = path.indexOf("#");
  const hash = hashIndex >= 0 ? path.slice(hashIndex) : "";
  const pathWithoutHash = hashIndex >= 0 ? path.slice(0, hashIndex) : path;
  const lineMatch = pathWithoutHash.match(/^(.*?)(:\d+)?$/);
  return {
    filePath: lineMatch?.[1] || pathWithoutHash,
    line: lineMatch?.[2]?.slice(1),
    hash,
  };
}

export function isUnsafeLink(href: string): boolean {
  const lower = href.trim().toLowerCase();
  return lower.startsWith("javascript:") || lower.startsWith("data:") || lower.startsWith("vbscript:");
}

export function normalizeDocumentHref(href: string): string {
  const raw = href.trim();
  if (!raw || isUnsafeLink(raw)) return "";

  // 1. Absolute host path mappings (/root/aads/...)
  const mappings = [...DOC_PATH_MAPPINGS].sort((a, b) => b.hostPrefix.length - a.hostPrefix.length);
  for (const mapping of mappings) {
    const prefix = trimTrailingSlash(mapping.hostPrefix);
    if (raw === prefix || raw.startsWith(`${prefix}/`)) {
      const { filePath, line, hash } = splitPathSuffix(raw.slice(prefix.length).replace(/^\/+/, ""));
      if (!filePath || filePath.includes("..")) return raw;
      return buildDocsHref(mapping.project, mapping.basePath, filePath, line, hash);
    }
  }

  // 2. Relative and container path mappings (docs/reports/..., /app/docs/...)
  for (const mapping of RELATIVE_DOC_MAPPINGS) {
    if (raw.startsWith(mapping.prefix)) {
      const remainder = raw.slice(mapping.stripPrefix.length);
      const { filePath, line, hash } = splitPathSuffix(remainder);
      if (!filePath || filePath.includes("..")) return raw;
      return buildDocsHref(mapping.project, mapping.basePath, filePath, line, hash);
    }
  }

  return raw;
}
