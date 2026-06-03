from web3 import Web3

from main.constants import STORY_AENEID_LICENSE_TOKEN_ADDRESS, STORY_AENEID_RPC_URL
from main.errors import LicenseVerificationError

OWNER_OF_ABI = [
    {
        "type": "function",
        "name": "ownerOf",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "owner", "type": "address"}],
    }
]


def normalize_owners(owner_wallets):
    owners = set()
    for wallet in owner_wallets:
        try:
            owners.add(Web3.to_checksum_address(wallet))
        except Exception:
            pass
    return owners


def verify_license_tokens_owned_by(owner_wallets, license_token_ids):
    if not license_token_ids:
        raise LicenseVerificationError(message="license_tokens_missing")
    owners = normalize_owners(owner_wallets)
    if not owners:
        raise LicenseVerificationError(message="buyer_wallet_missing")

    web3 = Web3(Web3.HTTPProvider(STORY_AENEID_RPC_URL, request_kwargs={"timeout": 15}))
    contract = web3.eth.contract(address=Web3.to_checksum_address(STORY_AENEID_LICENSE_TOKEN_ADDRESS), abi=OWNER_OF_ABI)
    for token_id in license_token_ids:
        if not str(token_id).isdigit():
            raise LicenseVerificationError(message="invalid_license_token_id")
        try:
            owner = Web3.to_checksum_address(contract.functions.ownerOf(int(token_id)).call())
        except Exception as exc:
            raise LicenseVerificationError(message=f"license_token_unverifiable:{token_id}") from exc
        if owner not in owners:
            raise LicenseVerificationError(message=f"license_token_not_owned_by_buyer:{token_id}")
