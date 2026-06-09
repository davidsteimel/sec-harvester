import httpx
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from config import AppConfig, SUBMISSIONS_URL, FACTS_URL, CONCEPT_URL


def _is_retryable(exc: BaseException) -> bool:
    """
    Checks if an exception is retryable based on its type and attributes.
    We consider HTTP status errors that indicate rate limiting or server issues as retryable,
    as well as network-related exceptions like timeouts.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        # 429 = Rate Limit Hit, 5xx = SEC Server Probleme
        return exc.response.status_code in {429, 500, 502, 503, 504}
    
    # retryable network issues: timeouts, connection errors, etc.
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
        
    return False


class EdgarClient:
    def __init__(self, config: AppConfig):
        self.config = config
        # rate limit: max 8 semaphores simultaneously
        self.semaphore = asyncio.Semaphore(8)

    @retry(
        stop=stop_after_attempt(5), 
        wait=wait_exponential(multiplier=1, min=4, max=60),
        retry=retry_if_exception(_is_retryable)
    )
    async def _fetch_json(self, client: httpx.AsyncClient, url: str) -> dict:
        async with self.semaphore:
            headers = {"User-Agent": self.config.user_agent}
            
            response = await client.get(url, headers=headers)
            response.raise_for_status() 
            return response.json()

    async def fetch_submissions(self, client: httpx.AsyncClient, cik: str) -> dict:
        url = SUBMISSIONS_URL.format(cik=cik)
        return await self._fetch_json(client, url)

    async def fetch_company_facts(self, client: httpx.AsyncClient, cik: str) -> dict:
        url = FACTS_URL.format(cik=cik)
        return await self._fetch_json(client, url)

    async def fetch_concept(self, client: httpx.AsyncClient, cik: str, taxonomy: str, tag: str) -> dict:
        url = CONCEPT_URL.format(cik=cik, tag=tag)
        return await self._fetch_json(client, url)