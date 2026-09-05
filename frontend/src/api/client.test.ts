import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'

import { ApiClient } from './client'
import { ApiClientError } from './errors'

import type {
  IncidentRequest,
  SubmitApprovalRequest,
} from './types'

function jsonResponse(
  body: unknown,
  status = 200,
): Response {
  return new Response(
    JSON.stringify(body),
    {
      status,
      headers: {
        'Content-Type': 'application/json',
      },
    },
  )
}

function incidentResponse() {
  return {
    incident_id: 'incident-123',
    thread_id: 'incident-123',
    phase: 'awaiting_approval',
    waiting_for_approval: true,
    request: {
      namespace: 'agent-demo',
      service_name: 'order-service',
      description: 'Service has no endpoints.',
    },
    evidence: [],
    retrieved_runbooks: [],
    errors: [],
    trace: [],
  }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('ApiClient', () => {
  it('creates an incident with JSON', async () => {
    const fetchMock = vi.fn(
      async (
        _input: RequestInfo | URL,
        _init?: RequestInit,
      ) => jsonResponse(incidentResponse(), 202),
    )
    const client = new ApiClient({
      fetcher: fetchMock,
    })
    const request: IncidentRequest = {
      namespace: 'agent-demo',
      service_name: 'order-service',
      description: 'Service has no endpoints.',
    }

    const result = await client.createIncident(request)

    expect(result.incident_id).toBe('incident-123')
    expect(fetchMock).toHaveBeenCalledOnce()

    const [url, init] = fetchMock.mock.calls[0]

    expect(url).toBe('/api/v1/incidents')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual(
      request,
    )

    const headers = new Headers(init?.headers)
    expect(headers.get('Accept')).toBe(
      'application/json',
    )
    expect(headers.get('Content-Type')).toBe(
      'application/json',
    )
  })

  it('submits an approval decision', async () => {
    const response = {
      ...incidentResponse(),
      phase: 'approval_rejected',
      waiting_for_approval: false,
    }
    const fetchMock = vi.fn(
      async (
        _input: RequestInfo | URL,
        _init?: RequestInit,
      ) => jsonResponse(response),
    )
    const client = new ApiClient({
      fetcher: fetchMock,
    })
    const request: SubmitApprovalRequest = {
      approval_id: 'apr-0123456789abcdef',
      approved: false,
      approver: 'test-operator',
      comment: 'Rejected.',
    }

    await client.submitApproval(
      'incident/unsafe id',
      request,
    )

    const [url, init] = fetchMock.mock.calls[0]

    expect(url).toBe(
      '/api/v1/incidents/'
      + 'incident%2Funsafe%20id/approval',
    )
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual(
      request,
    )
  })

  it('preserves a structured API error', async () => {
    const fetchMock = vi.fn(
      async (
        _input: RequestInfo | URL,
        _init?: RequestInit,
      ) => jsonResponse(
        {
          error: {
            code: 'APPROVAL_CONFLICT',
            message: 'Approval conflict.',
            details: {
              field: 'approval_id',
            },
          },
        },
        409,
      ),
    )
    const client = new ApiClient({
      fetcher: fetchMock,
    })

    await expect(
      client.getIncident('incident-123'),
    ).rejects.toMatchObject({
      name: 'ApiClientError',
      kind: 'http',
      status: 409,
      code: 'APPROVAL_CONFLICT',
      message: 'Approval conflict.',
      details: {
        field: 'approval_id',
      },
    })
  })

  it('handles an unstructured HTTP error', async () => {
    const fetchMock = vi.fn(
      async (
        _input: RequestInfo | URL,
        _init?: RequestInit,
      ) => jsonResponse(
        {
          message: 'Unexpected response',
        },
        503,
      ),
    )
    const client = new ApiClient({
      fetcher: fetchMock,
    })

    await expect(
      client.getHealth(),
    ).rejects.toMatchObject({
      kind: 'http',
      status: 503,
      code: 'HTTP_ERROR',
    })
  })

  it('rejects invalid JSON success responses', async () => {
    const fetchMock = vi.fn(
      async (
        _input: RequestInfo | URL,
        _init?: RequestInit,
      ) => new Response(
        'not-json',
        {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
          },
        },
      ),
    )
    const client = new ApiClient({
      fetcher: fetchMock,
    })

    await expect(
      client.getHealth(),
    ).rejects.toMatchObject({
      kind: 'invalid_response',
      code: 'INVALID_JSON_RESPONSE',
    })
  })

  it('rejects unexpected success payloads', async () => {
    const fetchMock = vi.fn(
      async (
        _input: RequestInfo | URL,
        _init?: RequestInit,
      ) => jsonResponse({
        status: 'ok',
      }),
    )
    const client = new ApiClient({
      fetcher: fetchMock,
    })

    await expect(
      client.getHealth(),
    ).rejects.toMatchObject({
      kind: 'invalid_response',
      code: 'INVALID_API_RESPONSE',
    })
  })

  it('wraps network failures', async () => {
    const networkError = new TypeError(
      'fetch failed',
    )
    const fetchMock = vi.fn(
      async (
        _input: RequestInfo | URL,
        _init?: RequestInit,
      ): Promise<Response> => {
        throw networkError
      },
    )
    const client = new ApiClient({
      fetcher: fetchMock,
    })

    try {
      await client.getHealth()
      throw new Error('Expected request to fail')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiClientError)
      expect(error).toMatchObject({
        kind: 'network',
        code: 'NETWORK_ERROR',
        originalError: networkError,
      })
    }
  })

  it('aborts requests after the timeout', async () => {
    vi.useFakeTimers()

    const fetchMock = vi.fn(
      (
        _input: RequestInfo | URL,
        init?: RequestInit,
      ) => new Promise<Response>(
        (_resolve, reject) => {
          init?.signal?.addEventListener(
            'abort',
            () => {
              reject(
                new DOMException(
                  'Aborted',
                  'AbortError',
                ),
              )
            },
          )
        },
      ),
    )
    const client = new ApiClient({
      fetcher: fetchMock,
      timeoutMs: 10,
    })

    const assertion = expect(
      client.getHealth(),
    ).rejects.toMatchObject({
      kind: 'timeout',
      code: 'REQUEST_TIMEOUT',
    })

    await vi.advanceTimersByTimeAsync(11)
    await assertion
  })
})