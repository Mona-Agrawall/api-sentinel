# Problem statement

Backend APIs regress in two quiet ways.

1. **Edge-case correctness.** Handlers mishandle nulls, missing optional fields,
   boundary numbers, wrong types and over-long strings. These cases are tedious to
   write by hand, so they are rarely covered. The result is a 500 in production where
   a 4xx was expected.
2. **Latency.** A change makes one endpoint slower. Without run-over-run comparison
   nobody notices until users do.

**Goal.** Given only an OpenAPI 3.x spec and a base URL, automatically:

- generate edge-case requests (deterministic rules, plus an LLM for creative cases),
- check that invalid input is rejected with 4xx and that nothing returns 5xx,
- measure per-endpoint latency under load (p50/p95/p99),
- store every run and flag new failures and statistically meaningful slowdowns
  against recent history,
- show it all on a dashboard.

**Non-goals.** Security or penetration testing, GraphQL, authenticated flows, and
contract testing of response bodies.
