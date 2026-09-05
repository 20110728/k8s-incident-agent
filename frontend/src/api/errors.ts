import type { JsonValue } from './types'

export type ApiErrorKind =
  | 'http'
  | 'network'
  | 'timeout'
  | 'invalid_response'

export interface ApiClientErrorOptions {
  kind: ApiErrorKind
  status?: number
  code?: string
  details?: JsonValue | null
  originalError?: unknown
}

export class ApiClientError extends Error {
  readonly kind: ApiErrorKind
  readonly status: number | null
  readonly code: string
  readonly details: JsonValue | null
  readonly originalError: unknown

  constructor(
    message: string,
    options: ApiClientErrorOptions,
  ) {
    super(message)

    this.name = 'ApiClientError'
    this.kind = options.kind
    this.status = options.status ?? null
    this.code = options.code ?? 'API_CLIENT_ERROR'
    this.details = options.details ?? null
    this.originalError = options.originalError
  }
}

export function isApiClientError(
  error: unknown,
): error is ApiClientError {
  return error instanceof ApiClientError
}