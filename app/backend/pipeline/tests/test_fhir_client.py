import httpx
from django.test import SimpleTestCase

from pipeline.services.fhir_client import (
    CircuitBreaker,
    CircuitOpen,
    FhirClient,
    PermanentHttpError,
    RetriesExhausted,
    UnsafeUrl,
)

BASE = "https://fhir.example.test/baseR4"


def scripted(*responses):
    calls = []
    queue = list(responses)

    def handler(request):
        calls.append(request)
        status, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, headers=headers, json={})

    return httpx.MockTransport(handler), calls


class FakeSleep:
    def __init__(self):
        self.waits = []

    async def __call__(self, seconds):
        self.waits.append(seconds)


class FhirClientTests(SimpleTestCase):
    async def make_client(self, transport, breaker=None):
        sleep = FakeSleep()
        client = FhirClient(BASE, http=httpx.AsyncClient(transport=transport), sleep=sleep, breaker=breaker)
        return client, sleep

    async def test_transient_error_is_retried_then_succeeds(self):
        transport, calls = scripted((503, {}), (200, {}))
        client, sleep = await self.make_client(transport)
        response = await client.get("Patient", step="test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleep.waits), 1)

    async def test_retry_after_header_is_honoured(self):
        transport, _ = scripted((429, {"Retry-After": "7"}), (200, {}))
        client, sleep = await self.make_client(transport)
        await client.get("Patient", step="test")
        self.assertEqual(sleep.waits, [7.0])

    async def test_permanent_error_fails_fast(self):
        transport, calls = scripted((403, {}))
        client, sleep = await self.make_client(transport)
        with self.assertRaises(PermanentHttpError) as ctx:
            await client.get("Patient", step="kick_off")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleep.waits, [])

    async def test_retries_are_capped(self):
        transport, calls = scripted((503, {}))
        client, _ = await self.make_client(transport)
        with self.assertRaises(RetriesExhausted):
            await client.get("Patient", step="test")
        self.assertEqual(len(calls), 5)

    async def test_circuit_opens_after_consecutive_failures(self):
        transport, calls = scripted((503, {}))
        client, _ = await self.make_client(transport, breaker=CircuitBreaker(threshold=3))
        with self.assertRaises(CircuitOpen):
            await client.get("Patient", step="test")
        self.assertEqual(len(calls), 3)

    async def test_success_resets_the_circuit(self):
        breaker = CircuitBreaker(threshold=3)
        transport, _ = scripted((503, {}), (503, {}), (200, {}))
        client, _ = await self.make_client(transport, breaker=breaker)
        await client.get("Patient", step="test")
        self.assertEqual(breaker.consecutive_failures, 0)

    async def test_links_to_other_hosts_are_rejected(self):
        transport, calls = scripted((200, {}))
        client, _ = await self.make_client(transport)
        with self.assertRaises(UnsafeUrl):
            await client.get("https://evil.example.test/baseR4/Binary/1", step="download")
        self.assertEqual(calls, [])
