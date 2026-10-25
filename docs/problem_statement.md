# Problem Statement

## Background

Most modern applications depend on backend APIs. Whenever an API changes, through a new
feature, a bug fix or a refactor, there is a risk that something which used to work no longer
does. Teams usually rely on automated tests to catch these problems. In practice, two kinds of
problem are often missed.

## The problem

### 1. Edge cases are rarely tested

Developers test the normal cases: valid input that should succeed. Unusual input is tested far
less often:

- a field sent as `null`,
- an optional field left out,
- a number just outside its allowed range,
- a value of the wrong type,
- a string that is empty or far too long.

Writing these tests by hand for every endpoint is slow and repetitive, so they are often
skipped. As a result, an API may crash with a `500 Internal Server Error` on input it should
have rejected cleanly with a `4xx` error. Users find these bugs in production.

### 2. Performance regressions go unnoticed

A code change can make an endpoint slower without breaking it. The response is still correct,
so functional tests pass. Unless latency is measured on every change and compared with earlier
measurements, the slowdown goes unnoticed until users complain.

## Objective

Build a tool that, given only an API's OpenAPI specification and its base URL, automatically:

1. **Generates edge-case tests** for every endpoint, using deterministic rules and,
   optionally, a large language model for additional cases.
2. **Runs those tests** and checks that invalid input is rejected with a `4xx` response and
   that no request causes a `5xx` server error.
3. **Load-tests the API** and measures response times (p50, p95 and p99) for each endpoint.
4. **Stores the results of every run** so that runs can be compared over time.
5. **Flags regressions**, meaning new server errors, newly failing tests, and significant
   slowdowns compared with recent runs under the same load.
6. **Presents the results** through a REST API, a command-line tool suitable for CI
   pipelines, and a dashboard.

## Success criteria

The tool is considered successful if, on a demo API with deliberately injected faults, it:

- detects an endpoint that has become slower and flags it as a latency regression,
- detects a request that now causes a server error and flags it,
- raises no false alarms on endpoints that have not changed,
- continues to work when the language model is unavailable, falling back to rule-based tests
  and recording that it did so.

## Scope

**In scope**

- REST APIs described by an OpenAPI 3.0 or 3.1 specification
- JSON request bodies, path parameters and query parameters
- Status-code checks, load testing and run-over-run comparison
- Safe operation: test traffic is sent only to approved hosts

**Out of scope**

- Security or penetration testing
- GraphQL, gRPC and Swagger 2.0 APIs
- Endpoints that require user authentication
- Validation of response body contents (contract testing)
