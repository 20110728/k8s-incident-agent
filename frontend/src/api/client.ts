import { ApiClientError } from './errors'

import type {
  ErrorResponse,
  HealthResponse,
  IncidentRequest,
  IncidentStatusResponse,
  ReadinessResponse,
  SubmitApprovalRequest,
} from './types'

export type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>

export interface ApiClientOptions {
  baseUrl?: string
  timeoutMs?: number
  fetcher?: FetchLike
}

type ResponseValidator<T> = (
  value: unknown,
) => value is T

const DEFAULT_TIMEOUT_MS = 180_000

function isRecord(
  value: unknown,
): value is Record<string, unknown> {
  return (
    typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
  )
}

function isErrorResponse(
  value: unknown,
): value is ErrorResponse {
  if (!isRecord(value) || !isRecord(value.error)) {
    return false
  }

  return (
    typeof value.error.code === 'string'
    && typeof value.error.message === 'string'
  )
}

function isHealthResponse(
  value: unknown,
): value is HealthResponse {
  return (
    isRecord(value)
    && value.status === 'ok'
    && typeof value.service === 'string'
    && typeof value.version === 'string'
  )
}

function isReadinessResponse(
  value: unknown,
): value is ReadinessResponse {
  return (
    isRecord(value)
    && value.status === 'ready'
    && isRecord(value.checks)
    && Object.values(value.checks).every(
      (check) => typeof check === 'boolean',
    )
  )
}

function isIncidentStatusResponse(
  value: unknown,
): value is IncidentStatusResponse {
  return (
    isRecord(value)
    && typeof value.incident_id === 'string'
    && typeof value.thread_id === 'string'
    && typeof value.phase === 'string'
    && typeof value.waiting_for_approval === 'boolean'
    && isRecord(value.request)
    && Array.isArray(value.evidence)
    && Array.isArray(value.retrieved_runbooks)
    && Array.isArray(value.errors)
    && Array.isArray(value.trace)
  )
}

function normalizeBaseUrl(value: string): string {
  const normalized = value.trim()

  if (normalized === '/') {
    return ''
  }

  return normalized.replace(/\/+$/, '')
}

function isAbortError(error: unknown): boolean {
  return (
    isRecord(error)
    && error.name === 'AbortError'
  )
}

export class ApiClient {
  private readonly baseUrl: string
  private readonly timeoutMs: number
  private readonly fetcher: FetchLike

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = normalizeBaseUrl(
      options.baseUrl ?? '',
    )
    this.timeoutMs =
      options.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.fetcher =
      options.fetcher
      ?? ((input, init) =>
        globalThis.fetch(input, init))
  }

  private async request<T>(
    path: string,
    init: RequestInit,
    validator: ResponseValidator<T>,
  ): Promise<T> {
    const controller = new AbortController()
    const timeoutId = globalThis.setTimeout(
      () => controller.abort(),
      this.timeoutMs,
    )

    const headers = new Headers(init.headers)
    headers.set('Accept', 'application/json')

    if (init.body !== undefined) {
      headers.set('Content-Type', 'application/json')
    }

    try {
      const response = await this.fetcher(
        `${this.baseUrl}${path}`,
        {
          ...init,
          headers,
          signal: controller.signal,
        },
      )

      let payload: unknown

      try {
        payload = await response.json()
      } catch (error) {
        if (!response.ok) {
          throw new ApiClientError(
            `Request failed with HTTP ${response.status}.`,
            {
              kind: 'http',
              status: response.status,
              code: 'HTTP_ERROR',
              originalError: error,
            },
          )
        }

        throw new ApiClientError(
          'The API returned invalid JSON.',
          {
            kind: 'invalid_response',
            status: response.status,
            code: 'INVALID_JSON_RESPONSE',
            originalError: error,
          },
        )
      }

      if (!response.ok) {
        if (isErrorResponse(payload)) {
          throw new ApiClientError(
            payload.error.message,
            {
              kind: 'http',
              status: response.status,
              code: payload.error.code,
              details: payload.error.details,
            },
          )
        }

        throw new ApiClientError(
          `Request failed with HTTP ${response.status}.`,
          {
            kind: 'http',
            status: response.status,
            code: 'HTTP_ERROR',
          },
        )
      }

      if (!validator(payload)) {
        throw new ApiClientError(
          'The API response did not match the expected contract.',
          {
            kind: 'invalid_response',
            status: response.status,
            code: 'INVALID_API_RESPONSE',
          },
        )
      }

      return payload
    } catch (error) {
      if (error instanceof ApiClientError) {
        throw error
      }

      if (
        controller.signal.aborted
        || isAbortError(error)
      ) {
        throw new ApiClientError(
          'The API request timed out.',
          {
            kind: 'timeout',
            code: 'REQUEST_TIMEOUT',
            originalError: error,
          },
        )
      }

      throw new ApiClientError(
        'Unable to reach the API.',
        {
          kind: 'network',
          code: 'NETWORK_ERROR',
          originalError: error,
        },
      )
    } finally {
      globalThis.clearTimeout(timeoutId)
    }
  }

  getHealth(): Promise<HealthResponse> {
    return this.request(
      '/healthz',
      {
        method: 'GET',
      },
      isHealthResponse,
    )
  }

  getReadiness(): Promise<ReadinessResponse> {
    return this.request(
      '/readyz',
      {
        method: 'GET',
      },
      isReadinessResponse,
    )
  }

  createIncident(
    request: IncidentRequest,
  ): Promise<IncidentStatusResponse> {
    return this.request(
      '/api/v1/incidents',
      {
        method: 'POST',
        body: JSON.stringify(request),
      },
      isIncidentStatusResponse,
    )
  }

  getIncident(
    incidentId: string,
  ): Promise<IncidentStatusResponse> {
    return this.request(
      `/api/v1/incidents/${
        encodeURIComponent(incidentId)
      }`,
      {
        method: 'GET',
      },
      isIncidentStatusResponse,
    )
  }

  submitApproval(
    incidentId: string,
    request: SubmitApprovalRequest,
  ): Promise<IncidentStatusResponse> {
    return this.request(
      `/api/v1/incidents/${
        encodeURIComponent(incidentId)
      }/approval`,
      {
        method: 'POST',
        body: JSON.stringify(request),
      },
      isIncidentStatusResponse,
    )
  }
}

export const apiClient = new ApiClient()