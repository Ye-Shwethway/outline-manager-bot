import logging
from outline_vpn.outline_vpn import OutlineVPN
from src.database.queries import get_server

logger = logging.getLogger(__name__)

def get_vpn_client(server_alias: str) -> OutlineVPN | None:
    """
    Fetches server credentials from the database and returns an authenticated client.
    Returns None if the server alias is not found or connection fails.
    """
    server_data = get_server(server_alias)
    if not server_data:
        logger.warning(f"Server alias '{server_alias}' not found in database.")
        return None
        
    try:
        # The library handles SSL verification securely via cert_sha256
        client = OutlineVPN(
            api_url=server_data['api_url'], 
            cert_sha256=server_data['cert_sha256']
        )
        return client
    except Exception as e:
        logger.error(f"Failed to connect to Outline Server '{server_alias}': {e}")
        return None