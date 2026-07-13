import logging
import requests
from outline_vpn.outline_vpn import OutlineVPN
from src.database.queries import get_server

logger = logging.getLogger(__name__)

# PATCH 2026-07-13: enforce a default timeout on every Outline API call.
# Previously the outline-vpn library called requests with timeout=None,
# so a half-open TCP connection (server reachable but never responds)
# would hang the bot's notifier thread forever, starving the scheduler
# and the python-telegram-bot update loop. Symptoms: bot stops
# responding to commands while still "running" in docker.
_DEFAULT_TIMEOUT = 15  # seconds, per request


def _wrap_with_default_timeout(client: OutlineVPN) -> OutlineVPN:
    """
    Monkey-patch the OutlineVPN instance so every library call gets a
    bounded timeout. Avoids touching the third-party library.
    """
    for method_name in (
        "get_keys",
        "get_key",
        "create_key",
        "delete_key",
        "rename_key",
        "add_data_limit",
        "delete_data_limit",
        "get_transferred_data",
        "get_server_information",
        "set_server_name",
    ):
        original = getattr(client, method_name, None)
        if original is None:
            continue

        def make_wrapper(orig, name):
            def wrapper(*args, **kwargs):
                # Only inject timeout if caller didn't already pass one.
                if "timeout" not in kwargs:
                    kwargs["timeout"] = _DEFAULT_TIMEOUT
                try:
                    return orig(*args, **kwargs)
                except (
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                ) as e:
                    logger.warning(
                        f"Outline API timeout/connection error on {name} "
                        f"(api_url={getattr(client, 'api_url', '?')}): {e}"
                    )
                    raise
            return wrapper

        setattr(client, method_name, make_wrapper(original, method_name))

    # Also tighten the session's HTTPAdapter retry policy: default 3 retries
    # with exponential backoff can mean a single dead server ties up the
    # notifier for several minutes even with our timeout.
    try:
        adapter = client.session.get_adapter(
            getattr(client, "api_url", "https://")
        )
        if hasattr(adapter, "max_retries"):
            from requests.adapters import DEFAULT_RETRIES

            adapter.max_retries = 0  # we already timeout-fail fast
            logger.debug(
                f"Set Outline session retries=0 for {getattr(client, 'api_url', '?')}"
            )
    except Exception as e:
        logger.debug(f"Could not adjust retry policy (non-fatal): {e}")

    return client


def get_vpn_client(server_alias: str) -> OutlineVPN | None:
    """
    Fetches server credentials from the database and returns an authenticated client
    with a default HTTP timeout applied to every API method.
    Returns None if the server alias is not found or connection fails.
    """
    server_data = get_server(server_alias)
    if not server_data:
        logger.warning(f"Server alias '{server_alias}' not found in database.")
        return None

    try:
        # The library handles SSL verification securely via cert_sha256
        client = OutlineVPN(
            api_url=server_data["api_url"],
            cert_sha256=server_data["cert_sha256"],
        )
        return _wrap_with_default_timeout(client)
    except Exception as e:
        logger.error(f"Failed to connect to Outline Server '{server_alias}': {e}")
        return None