import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = {
  params: {
    path?: string[];
  };
};

const API_BASE =
  process.env.API_PROXY_TARGET ||
  process.env.INTERNAL_API_ORIGIN ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://127.0.0.1:8002";

function buildBackendUrl(request: NextRequest, path: string[] | undefined) {
  const cleanPath = (path ?? []).map((part) => encodeURIComponent(part)).join("/");
  const base = API_BASE.replace(/\/$/, "");
  return `${base}/api/v4/chart/${cleanPath}${request.nextUrl.search}`;
}

function buildHeaders(request: NextRequest) {
  const internalKey = process.env.INTERNAL_API_KEY?.trim();
  if (!internalKey) return null;

  const headers = new Headers();
  headers.set("X-Internal-API-Key", internalKey);
  headers.set("Accept", request.headers.get("accept") || "application/json");

  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);

  const authorization = request.headers.get("authorization");
  if (authorization) headers.set("Authorization", authorization);

  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("Cookie", cookie);

  return headers;
}

async function proxyChartRequest(request: NextRequest, context: RouteContext) {
  const headers = buildHeaders(request);
  if (!headers) {
    return NextResponse.json(
      { detail: "Frontend INTERNAL_API_KEY is not configured" },
      { status: 503 },
    );
  }

  const method = request.method.toUpperCase();
  const init: RequestInit = {
    method,
    headers,
    cache: "no-store",
  };

  if (method !== "GET" && method !== "HEAD") {
    init.body = await request.text();
  }

  const backendResponse = await fetch(buildBackendUrl(request, context.params.path), init);
  const body = await backendResponse.arrayBuffer();
  const responseHeaders = new Headers();
  const contentType = backendResponse.headers.get("content-type");
  if (contentType) responseHeaders.set("Content-Type", contentType);
  responseHeaders.set("Cache-Control", "no-store, no-cache, must-revalidate");

  return new NextResponse(body, {
    status: backendResponse.status,
    statusText: backendResponse.statusText,
    headers: responseHeaders,
  });
}

export async function GET(request: NextRequest, context: RouteContext) {
  return proxyChartRequest(request, context);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxyChartRequest(request, context);
}
