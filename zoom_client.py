import email.utils
import time
from urllib.parse import urljoin

import requests

import utils


class ZoomClientError(Exception):
    def __init__(self, message, status_code=None, response_text=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class zoom_client:
    BASE_URL = "https://api.zoom.us/v2/"
    TOKEN_URL = "https://api.zoom.us/oauth/token"

    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        PAGE_SIZE: int = 300,
        timeout=(10, 120),
        max_rate_limit_retries: int = 3,
        sleep=time.sleep,
    ):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.PAGE_SIZE = PAGE_SIZE
        self.timeout = timeout
        self.max_rate_limit_retries = max_rate_limit_retries
        self.sleep = sleep
        self.cached_token = None

    def get(self, url, params=None):
        return self.request("GET", url, params=params).json()

    def delete(self, url, params=None):
        return self.request("DELETE", url, params=params)

    def request(self, method, url, params=None, json=None, stream=False):
        request_url = self._normalize_url(url)
        response = self._request_with_token(
            method, request_url, params=params, json=json, stream=stream
        )

        rate_limit_retries = 0
        while response.status_code == 429 and rate_limit_retries < self.max_rate_limit_retries:
            retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
            if retry_after > 0:
                self.sleep(retry_after)
            rate_limit_retries += 1
            response = self._request_with_token(
                method, request_url, params=params, json=json, stream=stream
            )

        if not response.ok:
            raise self._error_from_response(response)

        return response

    def _request_with_token(self, method, url, params=None, json=None, stream=False):
        token = self.cached_token or self.fetch_token()
        self.cached_token = token
        response = self._send(method, url, token, params=params, json=json, stream=stream)

        if response.status_code == 401:
            self.cached_token = self.fetch_token()
            response = self._send(
                method, url, self.cached_token, params=params, json=json, stream=stream
            )

        return response

    def _send(self, method, url, token, params=None, json=None, stream=False):
        return requests.request(
            method,
            url,
            headers=self.get_headers(token),
            params=params,
            json=json,
            stream=stream,
            timeout=self.timeout,
        )

    def fetch_token(self):
        data = {
            "grant_type": "account_credentials",
            "account_id": self.account_id,
        }
        response = requests.post(
            self.TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data=data,
            timeout=self.timeout,
        )

        try:
            payload = response.json()
        except ValueError as error:
            raise ZoomClientError(
                f"Unable to fetch access token: HTTP {response.status_code}"
            ) from error

        if not response.ok or "access_token" not in payload:
            reason = payload.get("reason") or payload.get("message") or response.text
            raise ZoomClientError(
                f"Unable to fetch access token: {utils.redact_sensitive_text(reason)}"
            )

        return payload["access_token"]

    def get_headers(self, token):
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def paginate(self, url):
        class __paginate_iter:
            def __init__(self, client, url):
                self.url = utils.add_url_params(url, {"page_size": client.PAGE_SIZE})
                self.client = client
                self.page = client.get(self.url)
                self.page_count = self.page.get("page_count") or 1
                self.page_token = self.page.get("next_page_token")

            def __iter__(self):
                return self

            def __len__(self):
                return self.page_count

            def __next__(self):
                page = self.page
                if not page and self.page_token:
                    page = self.client.get(
                        utils.add_url_params(
                            self.url, {"next_page_token": self.page_token}
                        )
                    )

                if not page:
                    raise StopIteration()

                self.page = None
                self.page_token = page.get("next_page_token")
                return page

        return __paginate_iter(self, url)

    def _normalize_url(self, url):
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return urljoin(self.BASE_URL, url.lstrip("/"))

    def _retry_after_seconds(self, value):
        if not value:
            return 0
        try:
            return max(0, int(value))
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return 0
            return max(0, int(retry_at.timestamp() - time.time()))

    def _error_from_response(self, response):
        text = utils.redact_sensitive_text(response.text)
        return ZoomClientError(
            f"Zoom API request failed: HTTP {response.status_code} {text}",
            status_code=response.status_code,
            response_text=text,
        )
