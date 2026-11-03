from ownmax.settings import DATA_DIR
import ipaddress, re


def clean(value):
    return re.sub("[\\x00-\\x1f\\x7f]", "", str(value or "")).strip()[:180]


def describe(writer, device_type=None, device_name=None, app_version=None):
    agent = getattr(writer, "_session_agent", {}) or {}
    name = clean(agent.get("deviceName") or device_name)
    platform = clean(agent.get("osVersion") or agent.get("platform"))
    try:
        headers = (
            getattr(writer, "request_headers", None)
            or getattr(getattr(writer, "request", None), "headers", {})
            or {}
        )
        ua = str(headers.get("User-Agent", ""))[:1024]
    except Exception:
        ua = ""
    browser = next(
        (
            label
            for fragment, label in (
                ("YaBrowser/", "Yandex Browser"),
                ("Edg/", "Microsoft Edge"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if fragment in ua
        ),
        "",
    )
    if ua:
        system = next(
            (
                label
                for fragment, label in (
                    ("iPhone", "iOS"),
                    ("iPad", "iPadOS"),
                    ("Android", "Android"),
                    ("Windows NT", "Windows"),
                    ("Macintosh", "macOS"),
                    ("Linux", "Linux"),
                )
                if fragment in ua
            ),
            "",
        )
        if system:
            platform = system
    parts = []
    for value in (browser or name, platform):
        if value and value.lower() not in [x.lower() for x in parts]:
            parts.append(value)
    return ", ".join(parts) or clean(device_type) or "MAX"


def geolocation(ip, path):
    import geoip2.database
    from pathlib import Path

    if not path or not Path(path).is_file():
        path = DATA_DIR / "GeoIP-Country.mmdb"
    try:
        address = ipaddress.ip_address(ip)
        if address.is_private or address.is_loopback:
            return "Локальная сеть"
        with geoip2.database.Reader(path) as reader:
            try:
                result = reader.city(str(address))
                parts = [result.country.name, result.subdivisions.most_specific.name]
                return ", ".join((v for v in parts if v)) or "Местоположение не определено"
            except Exception:
                result = reader.country(str(address))
                return result.country.name or "Местоположение не определено"
    except Exception:
        return "Местоположение не определено"


async def store(p, hashed, ip, writer, device_type=None, device_name=None, app_version=None):
    try:
        ip = str(ipaddress.ip_address(str(ip).removeprefix("::ffff:")))
    except ValueError:
        return
    info = describe(writer, device_type, device_name, app_version)
    location = geolocation(ip, p.config.geo_db_path)
    async with p.db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE tokens SET ip_address=%s,client_info=%s,location=%s WHERE token_hash=%s",
                (ip, info, location, hashed),
            )
