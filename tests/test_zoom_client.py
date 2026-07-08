import threading

import responses

from zoom_client import RateLimiter, zoom_client


def test_client_uses_a_single_pooled_session():
    client = zoom_client("acct", "client", "secret", concurrency=5)
    adapter = client.session.get_adapter("https://api.zoom.us/")
    assert adapter._pool_maxsize == 5


@responses.activate
def test_request_refreshes_token_once_after_401():
    responses.add(
        responses.POST,
        "https://api.zoom.us/oauth/token",
        json={"access_token": "token-one"},
        status=200,
    )
    responses.add(
        responses.POST,
        "https://api.zoom.us/oauth/token",
        json={"access_token": "token-two"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.zoom.us/v2/users/me",
        status=401,
        json={"message": "expired"},
    )
    responses.add(
        responses.GET,
        "https://api.zoom.us/v2/users/me",
        status=200,
        json={"id": "me"},
    )

    client = zoom_client("acct", "client", "secret", sleep=lambda seconds: None)

    assert client.get("/users/me") == {"id": "me"}
    auth_headers = [
        call.request.headers["Authorization"]
        for call in responses.calls
        if call.request.url == "https://api.zoom.us/v2/users/me"
    ]
    assert auth_headers == ["Bearer token-one", "Bearer token-two"]


@responses.activate
def test_request_honors_retry_after_for_rate_limit():
    sleeps = []
    responses.add(
        responses.POST,
        "https://api.zoom.us/oauth/token",
        json={"access_token": "token-one"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.zoom.us/v2/users/me",
        status=429,
        headers={"Retry-After": "2"},
        json={"message": "slow down"},
    )
    responses.add(
        responses.GET,
        "https://api.zoom.us/v2/users/me",
        status=200,
        json={"id": "me"},
    )

    client = zoom_client("acct", "client", "secret", sleep=sleeps.append,
                         requests_per_second=None, backoff_base=1.0,
                         backoff_jitter=lambda: 0)

    assert client.get("/users/me") == {"id": "me"}
    assert sleeps == [2]


def test_rate_limiter_spaces_calls():
    now = [0.0]
    sleeps = []
    limiter = RateLimiter(2, sleep=sleeps.append, monotonic=lambda: now[0])

    limiter.acquire()   # first call: no wait
    limiter.acquire()   # second call at same time: must wait 0.5s

    assert sleeps == [0.5]


@responses.activate
def test_backoff_uses_exponential_when_no_retry_after():
    sleeps = []
    responses.add(responses.POST, "https://api.zoom.us/oauth/token",
                  json={"access_token": "t"}, status=200)
    responses.add(responses.GET, "https://api.zoom.us/v2/users/me", status=429,
                  json={"message": "slow down"})
    responses.add(responses.GET, "https://api.zoom.us/v2/users/me", status=429,
                  json={"message": "slow down"})
    responses.add(responses.GET, "https://api.zoom.us/v2/users/me", status=200,
                  json={"id": "me"})

    client = zoom_client("acct", "client", "secret", sleep=sleeps.append,
                         requests_per_second=None, backoff_base=1.0,
                         backoff_jitter=lambda: 0)

    assert client.get("/users/me") == {"id": "me"}
    assert sleeps == [1.0, 2.0]  # 2**0, 2**1


@responses.activate
def test_concurrent_cold_start_requests_share_a_single_token_fetch():
    # Locks in the single-flight guarantee: many threads racing to make the
    # very first request (cached_token is None) must only trigger one
    # POST /oauth/token, not one per thread.
    responses.add(
        responses.POST,
        "https://api.zoom.us/oauth/token",
        json={"access_token": "token-one"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.zoom.us/v2/users/me",
        json={"id": "me"},
        status=200,
    )

    client = zoom_client("acct", "client", "secret", sleep=lambda seconds: None)
    assert client.cached_token is None

    thread_count = 20
    barrier = threading.Barrier(thread_count)
    results = []
    errors = []

    def worker():
        barrier.wait()  # line every thread up so they hit _get_token together
        try:
            results.append(client.get("/users/me"))
        except Exception as error:  # pragma: no cover - surfaced via assertion below
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == [{"id": "me"}] * thread_count

    token_calls = [
        call for call in responses.calls
        if call.request.url == "https://api.zoom.us/oauth/token"
    ]
    assert len(token_calls) == 1
