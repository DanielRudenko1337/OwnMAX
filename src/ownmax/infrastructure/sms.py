import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)


async def send_sms_code(gateway_url: str, phone: str) -> str | None:
    url = gateway_url.rstrip("/") + "/sms/send"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, json={"phone_number": phone}) as response:
                response.raise_for_status()
                data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        logger.exception("Could not reach SMS gateway")
        return None
    if not isinstance(data, dict) or not data.get("success"):
        logger.error("SMS gateway rejected the request")
        return None
    code = str(data.get("code", ""))
    if not code.isascii() or not code.isdigit() or len(code) not in (5, 6):
        logger.error("SMS gateway returned an invalid code")
        return None
    return code
