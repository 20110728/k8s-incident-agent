import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'

import { ApiClient } from './client'

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

describe('ApiClient browser fetch compatibility', () => {
  it('calls the default fetch through globalThis', async () => {
    const fetcher = vi.fn(function (
      this: unknown,
    ) {
      expect(this).toBe(globalThis)

      return Promise.resolve(
        new Response(
          JSON.stringify({
            status: 'ok',
            service: 'k8s-incident-agent',
            version: 'test',
          }),
          {
            status: 200,
            headers: {
              'Content-Type': 'application/json',
            },
          },
        ),
      )
    })

    globalThis.fetch =
      fetcher as typeof globalThis.fetch

    const client = new ApiClient()
    const result = await client.getHealth()

    expect(result).toEqual({
      status: 'ok',
      service: 'k8s-incident-agent',
      version: 'test',
    })
    expect(fetcher).toHaveBeenCalledOnce()
  })
})