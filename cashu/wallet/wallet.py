import copy
import json
import time
import uuid
from posixpath import join
from typing import Dict, List, Optional, Tuple, Union

import bolt11
import httpx
from bip32 import BIP32
from httpx import Response
from loguru import logger

from ..core.base import (
    Amount,
    BlindedMessage,
    BlindedSignature,
    DLEQWallet,
    Invoice,
    Proof,
    ProofState,
    SpentState,
    Unit,
    WalletKeyset,
)
from ..core.crypto import b_dhke
from ..core.crypto.secp import PrivateKey, PublicKey
from ..core.db import Database
from ..core.errors import KeysetNotFoundError
from ..core.helpers import calculate_number_of_blank_outputs, sum_proofs
from ..core.migrations import migrate_databases
from ..core.models import (
    CheckFeesResponse_deprecated,
    GetInfoResponse,
    KeysetsResponse,
    KeysetsResponseKeyset,
    KeysResponse,
    PostCheckStateRequest,
    PostCheckStateResponse,
    PostMeltQuoteRequest,
    PostMeltQuoteResponse,
    PostMeltRequest,
    PostMeltResponse,
    PostMeltResponse_deprecated,
    PostMintQuoteRequest,
    PostMintQuoteResponse,
    PostMintRequest,
    PostMintResponse,
    PostRestoreResponse,
    PostSplitRequest,
    PostSplitResponse,
)
from ..core.p2pk import Secret
from ..core.settings import settings
from ..core.split import amount_split
from ..tor.tor import TorProxy
from ..wallet.crud import (
    bump_secret_derivation,
    get_keysets,
    get_lightning_invoice,
    get_proofs,
    invalidate_proof,
    secret_used,
    set_secret_derivation,
    store_keyset,
    store_lightning_invoice,
    store_proof,
    update_keyset,
    update_lightning_invoice,
    update_proof,
)
from . import migrations
from .htlc import WalletHTLC
from .mint_info import MintInfo
from .p2pk import WalletP2PK
from .proofs import WalletProofs
from .secrets import WalletSecrets
from .transactions import WalletTransactions
from .wallet_deprecated import LedgerAPIDeprecated


def async_set_httpx_client(func):
    """
    Decorator that wraps around any async class method of LedgerAPI that makes
    API calls. Sets some HTTP headers and starts a Tor instance if none is
    already running and and sets local proxy to use it.
    """

    async def wrapper(self, *args, **kwargs):
        # set proxy
        proxies_dict = {}
        proxy_url: Union[str, None] = None
        if settings.tor and TorProxy().check_platform():
            self.tor = TorProxy(timeout=True)
            self.tor.run_daemon(verbose=True)
            proxy_url = "socks5://localhost:9050"
        elif settings.socks_proxy:
            proxy_url = f"socks5://{settings.socks_proxy}"
        elif settings.http_proxy:
            proxy_url = settings.http_proxy
        if proxy_url:
            proxies_dict.update({"all://": proxy_url})

        headers_dict = {"Client-version": settings.version}

        self.httpx = httpx.AsyncClient(
            verify=not settings.debug,
            proxies=proxies_dict,  # type: ignore
            headers=headers_dict,
            base_url=self.url,
            timeout=None if settings.debug else 60,
        )
        return await func(self, *args, **kwargs)

    return wrapper


def async_ensure_mint_loaded(func):
    """Decorator that ensures that the mint is loaded before calling the wrapped
    function. If the mint is not loaded, it will be loaded first.
    """

    async def wrapper(self, *args, **kwargs):
        if not self.keysets:
            await self.load_mint()
        return await func(self, *args, **kwargs)

    return wrapper


class LedgerAPI(LedgerAPIDeprecated, object):
    tor: TorProxy
    db: Database  # we need the db for melt_deprecated
    httpx: httpx.AsyncClient

    def __init__(self, url: str, db: Database):
        self.url = url
        self.db = db

    @async_set_httpx_client
    async def _init_s(self):
        """Dummy function that can be called from outside to use LedgerAPI.s"""
        return

    @staticmethod
    def raise_on_error_request(
        resp: Response,
    ) -> None:
        """Raises an exception if the response from the mint contains an error.

        Args:
            resp_dict (Response): Response dict (previously JSON) from mint

        Raises:
            Exception: if the response contains an error
        """
        try:
            resp_dict = resp.json()
        except json.JSONDecodeError:
            # if we can't decode the response, raise for status
            resp.raise_for_status()
            return
        if "detail" in resp_dict:
            logger.trace(f"Error from mint: {resp_dict}")
            error_message = f"Mint Error: {resp_dict['detail']}"
            if "code" in resp_dict:
                error_message += f" (Code: {resp_dict['code']})"
            raise Exception(error_message)
        # raise for status if no error
        resp.raise_for_status()

    # async def activate_keyset(self, keyset_id: Optional[str] = None) -> None:
    #     """Loads keys from mint and stores them in the database.

    #     Args:
    #         keyset_id (str, optional): keyset id to load. If given, requests keys for this keyset
    #         from the mint. If not given, requests current keyset of the mint. Defaults to "".

    #     Raises:
    #         AssertionError: if mint URL is not set
    #         AssertionError: if no keys are received from the mint
    #     """
    #     logger.trace(f"Loading mint keys: {keyset_id}")
    #     assert len(
    #         self.url
    #     ), "Ledger not initialized correctly: mint URL not specified yet. "

    #     keyset: WalletKeyset

    #     # if we want to load a specific keyset
    #     if keyset_id:
    #         # check if this keyset is in db
    #         logger.trace(f"Loading keyset {keyset_id} from database.")
    #         keysets = await get_keysets(keyset_id, db=self.db)
    #         if keysets:
    #             logger.debug(f"Found keyset {keyset_id} in database.")
    #             # select as current keyset
    #             keyset = keysets[0]
    #         else:
    #             logger.trace(
    #                 f"Could not find keyset {keyset_id} in database. Loading keyset"
    #                 " from mint."
    #             )
    #             keyset = await self._get_keyset(keyset_id)
    #             if keyset.id == keyset_id:
    #                 # NOTE: Derived keyset *could* have a different id than the one
    #                 # requested because of the duplicate keysets for < 0.15.0 that's
    #                 # why we make an explicit check here to not overwrite an existing
    #                 # keyset with the incoming one.
    #                 logger.debug(
    #                     f"Storing new mint keyset: {keyset.id} ({keyset.unit.name})"
    #                 )
    #                 await store_keyset(keyset=keyset, db=self.db)
    #             keysets = [keyset]
    #     else:
    #         # else we load all active keysets of the mint and choose
    #         # an appropriate one as the current keyset
    #         keysets = await self._get_keys()
    #         assert len(keysets), Exception("did not receive any keys")
    #         # check if we have all keysets in db
    #         for keyset in keysets:
    #             keysets_in_db = await get_keysets(keyset.id, db=self.db)
    #             if not keysets_in_db:
    #                 logger.debug(
    #                     "Storing new current mint keyset:"
    #                     f" {keyset.id} ({keyset.unit.name})"
    #                 )
    #                 await store_keyset(keyset=keyset, db=self.db)

    #         # select a keyset that matches the wallet unit
    #         wallet_unit_keysets = [k for k in keysets if k.unit == self.unit]
    #         assert len(wallet_unit_keysets) > 0, f"no keyset for unit {self.unit.name}."
    #         keyset = [k for k in keysets if k.unit == self.unit][0]

    #     # load all keysets we have into memory
    #     for k in keysets:
    #         self.keysets[k.id] = k

    #     # make sure we have selected a current keyset
    #     assert keyset
    #     assert keyset.id
    #     assert len(keyset.public_keys) > 0, "no public keys in keyset"
    #     # set current keyset id
    #     self.keyset_id = keyset.id
    #     logger.debug(f"Current mint keyset: {self.keyset_id}")

    # async def load_mint_keysets(self) -> List[str]:
    #     """Loads the keyset IDs of the mint.

    #     Returns:
    #         List[str]: list of keyset IDs of the mint

    #     Raises:
    #         AssertionError: if no keysets are received from the mint
    #     """
    #     logger.trace("Loading mint keysets.")
    #     mint_keysets = []
    #     try:
    #         mint_keysets = await self._get_keysets()
    #     except Exception:
    #         assert self.keysets[
    #             self.keyset_id
    #         ].id, "could not get keysets from mint, and do not have keys"
    #         pass
    #     self.mint_keyset_ids = [k.id for k in mint_keysets] or [
    #         self.keysets[self.keyset_id].id
    #     ]
    #     logger.debug(f"Mint keysets: {self.mint_keyset_ids}")
    #     return self.mint_keyset_ids

    # async def load_mint_info(self) -> MintInfo:
    #     """Loads the mint info from the mint."""
    #     mint_info_resp = await self._get_info()
    #     self.mint_info = MintInfo(**mint_info_resp.dict())
    #     logger.debug(f"Mint info: {self.mint_info}")
    #     return self.mint_info

    """
    ENDPOINTS
    """

    @async_set_httpx_client
    async def _get_keys(self) -> List[WalletKeyset]:
        """API that gets the current keys of the mint

        Args:
            url (str): Mint URL

        Returns:
            WalletKeyset: Current mint keyset

        Raises:
            Exception: If no keys are received from the mint
        """
        resp = await self.httpx.get(
            join(self.url, "/v1/keys"),
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self._get_keys_deprecated(self.url)
            return [ret]
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        keys_dict: dict = resp.json()
        assert len(keys_dict), Exception("did not receive any keys")
        keys = KeysResponse.parse_obj(keys_dict)
        logger.debug(
            f"Received {len(keys.keysets)} keysets from mint:"
            f" {' '.join([k.id + f' ({k.unit})' for k in keys.keysets])}."
        )
        ret = [
            WalletKeyset(
                id=keyset.id,
                unit=keyset.unit,
                public_keys={
                    int(amt): PublicKey(bytes.fromhex(val), raw=True)
                    for amt, val in keyset.keys.items()
                },
                mint_url=self.url,
            )
            for keyset in keys.keysets
        ]
        return ret

    @async_set_httpx_client
    async def _get_keyset(self, keyset_id: str) -> WalletKeyset:
        """API that gets the keys of a specific keyset from the mint.


        Args:
            keyset_id (str): base64 keyset ID, needs to be urlsafe-encoded before sending to mint (done in this method)

        Returns:
            WalletKeyset: Keyset with ID keyset_id

        Raises:
            Exception: If no keys are received from the mint
        """
        keyset_id_urlsafe = keyset_id.replace("+", "-").replace("/", "_")
        resp = await self.httpx.get(
            join(self.url, f"/v1/keys/{keyset_id_urlsafe}"),
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self._get_keyset_deprecated(self.url, keyset_id)
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)

        keys_dict = resp.json()
        assert len(keys_dict), Exception("did not receive any keys")
        keys = KeysResponse.parse_obj(keys_dict)
        this_keyset = keys.keysets[0]
        keyset_keys = {
            int(amt): PublicKey(bytes.fromhex(val), raw=True)
            for amt, val in this_keyset.keys.items()
        }
        keyset = WalletKeyset(
            id=keyset_id,
            unit=this_keyset.unit,
            public_keys=keyset_keys,
            mint_url=self.url,
        )
        return keyset

    @async_set_httpx_client
    async def _get_keysets(self) -> List[KeysetsResponseKeyset]:
        """API that gets a list of all active keysets of the mint.

        Returns:
            KeysetsResponse (List[str]): List of all active keyset IDs of the mint

        Raises:
            Exception: If no keysets are received from the mint
        """
        resp = await self.httpx.get(
            join(self.url, "/v1/keysets"),
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self._get_keysets_deprecated(self.url)
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)

        keysets_dict = resp.json()
        keysets = KeysetsResponse.parse_obj(keysets_dict).keysets
        if not keysets:
            raise Exception("did not receive any keysets")
        return keysets

    @async_set_httpx_client
    async def _get_info(self) -> GetInfoResponse:
        """API that gets the mint info.

        Returns:
            GetInfoResponse: Current mint info

        Raises:
            Exception: If the mint info request fails
        """
        resp = await self.httpx.get(
            join(self.url, "/v1/info"),
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self._get_info_deprecated()
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        data: dict = resp.json()
        mint_info: GetInfoResponse = GetInfoResponse.parse_obj(data)
        return mint_info

    @async_set_httpx_client
    @async_ensure_mint_loaded
    async def mint_quote(self, amount: int, unit: Unit) -> PostMintQuoteResponse:
        """Requests a mint quote from the server and returns a payment request.

        Args:
            amount (int): Amount of tokens to mint

        Returns:
            PostMintQuoteResponse: Mint Quote Response

        Raises:
            Exception: If the mint request fails
        """
        logger.trace("Requesting mint: GET /v1/mint/bolt11")
        payload = PostMintQuoteRequest(unit=unit.name, amount=amount)
        resp = await self.httpx.post(
            join(self.url, "/v1/mint/quote/bolt11"), json=payload.dict()
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self.request_mint_deprecated(amount)
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        return_dict = resp.json()
        return PostMintQuoteResponse.parse_obj(return_dict)

    @async_set_httpx_client
    @async_ensure_mint_loaded
    async def mint(
        self, outputs: List[BlindedMessage], quote: str
    ) -> List[BlindedSignature]:
        """Mints new coins and returns a proof of promise.

        Args:
            outputs (List[BlindedMessage]): Outputs to mint new tokens with
            quote (str): Quote ID.

        Returns:
            list[Proof]: List of proofs.

        Raises:
            Exception: If the minting fails
        """
        outputs_payload = PostMintRequest(outputs=outputs, quote=quote)
        logger.trace("Checking Lightning invoice. POST /v1/mint/bolt11")

        def _mintrequest_include_fields(outputs: List[BlindedMessage]):
            """strips away fields from the model that aren't necessary for the /mint"""
            outputs_include = {"id", "amount", "B_"}
            return {
                "quote": ...,
                "outputs": {i: outputs_include for i in range(len(outputs))},
            }

        payload = outputs_payload.dict(include=_mintrequest_include_fields(outputs))  # type: ignore
        resp = await self.httpx.post(
            join(self.url, "/v1/mint/bolt11"),
            json=payload,  # type: ignore
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self.mint_deprecated(outputs, quote)
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        response_dict = resp.json()
        logger.trace("Lightning invoice checked. POST /v1/mint/bolt11")
        promises = PostMintResponse.parse_obj(response_dict).signatures
        return promises

    @async_set_httpx_client
    @async_ensure_mint_loaded
    async def melt_quote(
        self, payment_request: str, unit: Unit, amount: Optional[int] = None
    ) -> PostMeltQuoteResponse:
        """Checks whether the Lightning payment is internal."""
        invoice_obj = bolt11.decode(payment_request)
        assert invoice_obj.amount_msat, "invoice must have amount"
        payload = PostMeltQuoteRequest(
            unit=unit.name, request=payment_request, amount=amount
        )
        resp = await self.httpx.post(
            join(self.url, "/v1/melt/quote/bolt11"),
            json=payload.dict(),
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret: CheckFeesResponse_deprecated = await self.check_fees_deprecated(
                payment_request
            )
            quote_id = "deprecated_" + str(uuid.uuid4())
            return PostMeltQuoteResponse(
                quote=quote_id,
                amount=amount or invoice_obj.amount_msat // 1000,
                fee_reserve=ret.fee or 0,
                paid=False,
                expiry=invoice_obj.expiry,
            )
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        return_dict = resp.json()
        return PostMeltQuoteResponse.parse_obj(return_dict)

    @async_set_httpx_client
    @async_ensure_mint_loaded
    async def melt(
        self,
        quote: str,
        proofs: List[Proof],
        outputs: Optional[List[BlindedMessage]],
    ) -> PostMeltResponse:
        """
        Accepts proofs and a lightning invoice to pay in exchange.
        """

        payload = PostMeltRequest(quote=quote, inputs=proofs, outputs=outputs)

        def _meltrequest_include_fields(
            proofs: List[Proof], outputs: List[BlindedMessage]
        ):
            """strips away fields from the model that aren't necessary for the /melt"""
            proofs_include = {"id", "amount", "secret", "C", "witness"}
            outputs_include = {"id", "amount", "B_"}
            return {
                "quote": ...,
                "inputs": {i: proofs_include for i in range(len(proofs))},
                "outputs": {i: outputs_include for i in range(len(outputs))},
            }

        resp = await self.httpx.post(
            join(self.url, "/v1/melt/bolt11"),
            json=payload.dict(include=_meltrequest_include_fields(proofs, outputs)),  # type: ignore
            timeout=None,
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            invoice = await get_lightning_invoice(id=quote, db=self.db)
            assert invoice, f"no invoice found for id {quote}"
            ret: PostMeltResponse_deprecated = await self.melt_deprecated(
                proofs=proofs, outputs=outputs, invoice=invoice.bolt11
            )
            return PostMeltResponse(
                paid=ret.paid, payment_preimage=ret.preimage, change=ret.change
            )
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        return_dict = resp.json()
        return PostMeltResponse.parse_obj(return_dict)

    @async_set_httpx_client
    @async_ensure_mint_loaded
    async def split(
        self,
        proofs: List[Proof],
        outputs: List[BlindedMessage],
    ) -> List[BlindedSignature]:
        """Consume proofs and create new promises based on amount split."""
        logger.debug("Calling split. POST /v1/swap")
        split_payload = PostSplitRequest(inputs=proofs, outputs=outputs)

        # construct payload
        def _splitrequest_include_fields(proofs: List[Proof]):
            """strips away fields from the model that aren't necessary for /v1/swap"""
            proofs_include = {
                "id",
                "amount",
                "secret",
                "C",
                "witness",
            }
            return {
                "outputs": ...,
                "inputs": {i: proofs_include for i in range(len(proofs))},
            }

        resp = await self.httpx.post(
            join(self.url, "/v1/swap"),
            json=split_payload.dict(include=_splitrequest_include_fields(proofs)),  # type: ignore
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self.split_deprecated(proofs, outputs)
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        promises_dict = resp.json()
        mint_response = PostSplitResponse.parse_obj(promises_dict)
        promises = [BlindedSignature(**p.dict()) for p in mint_response.signatures]

        if len(promises) == 0:
            raise Exception("received no splits.")

        return promises

    @async_set_httpx_client
    @async_ensure_mint_loaded
    async def check_proof_state(self, proofs: List[Proof]) -> PostCheckStateResponse:
        """
        Checks whether the secrets in proofs are already spent or not and returns a list of booleans.
        """
        payload = PostCheckStateRequest(Ys=[p.Y for p in proofs])
        resp = await self.httpx.post(
            join(self.url, "/v1/checkstate"),
            json=payload.dict(),
        )
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self.check_proof_state_deprecated(proofs)
            # convert CheckSpendableResponse_deprecated to CheckSpendableResponse
            states: List[ProofState] = []
            for spendable, pending, p in zip(ret.spendable, ret.pending, proofs):
                if spendable and not pending:
                    states.append(ProofState(Y=p.Y, state=SpentState.unspent))
                elif spendable and pending:
                    states.append(ProofState(Y=p.Y, state=SpentState.pending))
                else:
                    states.append(ProofState(Y=p.Y, state=SpentState.spent))
            ret = PostCheckStateResponse(states=states)
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        return PostCheckStateResponse.parse_obj(resp.json())

    @async_set_httpx_client
    @async_ensure_mint_loaded
    async def restore_promises(
        self, outputs: List[BlindedMessage]
    ) -> Tuple[List[BlindedMessage], List[BlindedSignature]]:
        """
        Asks the mint to restore promises corresponding to outputs.
        """
        payload = PostMintRequest(quote="restore", outputs=outputs)
        resp = await self.httpx.post(join(self.url, "/v1/restore"), json=payload.dict())
        # BEGIN backwards compatibility < 0.15.0
        # assume the mint has not upgraded yet if we get a 404
        if resp.status_code == 404:
            ret = await self.restore_promises_deprecated(outputs)
            return ret
        # END backwards compatibility < 0.15.0
        self.raise_on_error_request(resp)
        response_dict = resp.json()
        returnObj = PostRestoreResponse.parse_obj(response_dict)

        # BEGIN backwards compatibility < 0.15.1
        # if the mint returns promises, duplicate into signatures
        if returnObj.promises:
            returnObj.signatures = returnObj.promises
        # END backwards compatibility < 0.15.1

        return returnObj.outputs, returnObj.signatures


class Wallet(
    LedgerAPI, WalletP2PK, WalletHTLC, WalletSecrets, WalletTransactions, WalletProofs
):
    """Minimal wallet wrapper."""

    keyset_id: str  # holds current keyset id
    keysets: Dict[str, WalletKeyset]  # holds keysets
    # mint_keyset_ids: List[str]  # holds active keyset ids of the mint
    unit: Unit
    mint_info: MintInfo  # holds info about mint
    mnemonic: str  # holds mnemonic of the wallet
    seed: bytes  # holds private key of the wallet generated from the mnemonic
    db: Database
    bip32: BIP32
    # private_key: Optional[PrivateKey] = None

    def __init__(self, url: str, db: str, name: str = "no_name", unit: str = "sat"):
        """A Cashu wallet.

        Args:
            url (str): URL of the mint.
            db (str): Path to the database directory.
            name (str, optional): Name of the wallet database file. Defaults to "no_name".
        """
        self.db = Database("wallet", db)
        self.proofs: List[Proof] = []
        self.name = name
        self.unit = Unit[unit]

        super().__init__(url=url, db=self.db)
        logger.debug("Wallet initialized")
        logger.debug(f"Mint URL: {url}")
        logger.debug(f"Database: {db}")
        logger.debug(f"Unit: {self.unit.name}")

    @classmethod
    async def with_db(
        cls,
        url: str,
        db: str,
        name: str = "no_name",
        skip_db_read: bool = False,
        unit: str = "sat",
    ):
        """Initializes a wallet with a database and initializes the private key.

        Args:
            url (str): URL of the mint.
            db (str): Path to the database.
            name (str, optional): Name of the wallet. Defaults to "no_name".
            skip_db_read (bool, optional): If true, values from db like private key and
                keysets are not loaded. Useful for running only migrations and returning.
                Defaults to False.

        Returns:
            Wallet: Initialized wallet.
        """
        logger.trace(f"Initializing wallet with database: {db}")
        self = cls(url=url, db=db, name=name, unit=unit)
        await self._migrate_database()
        if not skip_db_read:
            logger.trace("Mint init: loading private key and keysets from db.")
            await self._init_private_key()
            keysets_list = await get_keysets(mint_url=url, db=self.db)
            keysets_active_unit = [k for k in keysets_list if k.unit == self.unit]
            self.keysets = {k.id: k for k in keysets_active_unit}
            logger.debug(
                f"Loaded keysets: {' '.join([k.id + f' {k.unit}' for k in keysets_active_unit])}"
            )

        return self

    async def _migrate_database(self):
        try:
            await migrate_databases(self.db, migrations)
        except Exception as e:
            logger.error(f"Could not run migrations: {e}")
            raise e

    # ---------- API ----------

    async def load_mint_info(self) -> MintInfo:
        """Loads the mint info from the mint."""
        mint_info_resp = await self._get_info()
        self.mint_info = MintInfo(**mint_info_resp.dict())
        logger.debug(f"Mint info: {self.mint_info}")
        return self.mint_info

    async def load_mint_keysets(self):
        """Loads all keyset of the mint and makes sure we have them all in the database.

        Then loads all keysets from the database for the active mint and active unit into self.keysets.
        """
        logger.trace("Loading mint keysets.")
        mint_keysets_resp = await self._get_keysets()
        mint_keysets_dict = {k.id: k for k in mint_keysets_resp}

        # load all keysets of thisd mint from the db
        keysets_in_db = await get_keysets(mint_url=self.url, db=self.db)

        # db is empty, get all keys from the mint and store them
        if not keysets_in_db:
            all_keysets = await self._get_keys()
            for keyset in all_keysets:
                keyset.active = mint_keysets_dict[keyset.id].active
                keyset.input_fee_ppk = mint_keysets_dict[keyset.id].input_fee_ppk or 0
                await store_keyset(keyset=keyset, db=self.db)

        keysets_in_db = await get_keysets(mint_url=self.url, db=self.db)
        keysets_in_db_dict = {k.id: k for k in keysets_in_db}

        # get all new keysets that are not in memory yet and store them in the database
        for mint_keyset in mint_keysets_dict.values():
            if mint_keyset.id not in keysets_in_db_dict:
                logger.debug(
                    f"Storing new mint keyset: {mint_keyset.id} ({mint_keyset.unit})"
                )
                wallet_keyset = await self._get_keyset(mint_keyset.id)
                wallet_keyset.active = mint_keyset.active
                wallet_keyset.input_fee_ppk = mint_keyset.input_fee_ppk or 0
                await store_keyset(keyset=wallet_keyset, db=self.db)

            # if the active attribute has changed, update it in the database
            if (
                mint_keyset.id in keysets_in_db_dict
                and mint_keyset.active != keysets_in_db_dict[mint_keyset.id].active
            ):
                await update_keyset(
                    keyset=keysets_in_db_dict[mint_keyset.id], db=self.db
                )

        await self.load_keysets_from_db()

    async def activate_keyset(self, keyset_id: Optional[str] = None) -> None:
        """Activates a keyset by setting self.keyset_id. Either activates a specific keyset
        of chooses one of the active keysets of the mint with the same unit as the wallet.
        """

        if keyset_id:
            if keyset_id not in self.keysets:
                await self.load_mint_keysets()

            if keyset_id not in self.keysets:
                raise KeysetNotFoundError(keyset_id)

            if self.keysets[keyset_id].unit != self.unit:
                raise Exception(
                    f"Keyset {keyset_id} has unit {self.keysets[keyset_id].unit.name},"
                    f" but wallet has unit {self.unit.name}."
                )

            if not self.keysets[keyset_id].active:
                raise Exception(f"Keyset {keyset_id} is not active.")

            self.keyset_id = keyset_id
        else:
            # if no keyset_id is given, choose an active keyset with the same unit as the wallet
            chosen_keyset = None
            for keyset in self.keysets.values():
                if keyset.unit == self.unit and keyset.active:
                    chosen_keyset = keyset
                    break

            if not chosen_keyset:
                raise Exception(f"No active keyset found for unit {self.unit.name}.")

            self.keyset_id = chosen_keyset.id

        logger.debug(f"Activated keyset {self.keyset_id}")

    async def load_mint(self, keyset_id: str = "") -> None:
        """
        Loads the public keys of the mint. Either gets the keys for the specified
        `keyset_id` or gets the keys of the active keyset from the mint.
        Gets the active keyset ids of the mint and stores in `self.mint_keyset_ids`.
        """
        logger.trace("Loading mint.")
        await self.load_mint_keysets()
        await self.activate_keyset(keyset_id)
        try:
            await self.load_mint_info()
        except Exception as e:
            logger.debug(f"Could not load mint info: {e}")
            pass

    async def load_proofs(self, reload: bool = False) -> None:
        """Load all proofs of the selected mint and unit (i.e. self.keysets) into memory."""

        if self.proofs and not reload:
            logger.debug("Proofs already loaded.")
            return

        self.proofs = []
        await self.load_keysets_from_db()
        async with self.db.connect() as conn:
            for keyset_id in self.keysets:
                proofs = await get_proofs(db=self.db, id=keyset_id, conn=conn)
                self.proofs.extend(proofs)
        logger.trace(
            f"Proofs loaded for keysets: {' '.join([k.id + f' ({k.unit})' for k in self.keysets.values()])}"
        )

    async def load_keysets_from_db(self):
        """Load all keysets of the selected mint and unit from the database into self.keysets."""
        keysets = await get_keysets(mint_url=self.url, unit=self.unit.name, db=self.db)
        for keyset in keysets:
            self.keysets[keyset.id] = keyset
        logger.debug(f"Keysets: {[k.id for k in self.keysets.values()]}")

    async def _check_used_secrets(self, secrets):
        """Checks if any of the secrets have already been used"""
        logger.trace("Checking secrets.")
        for s in secrets:
            if await secret_used(s, db=self.db):
                raise Exception(f"secret already used: {s}")
        logger.trace("Secret check complete.")

    async def request_mint(self, amount: int) -> Invoice:
        """Request a Lightning invoice for minting tokens.

        Args:
            amount (int): Amount for Lightning invoice in satoshis

        Returns:
            PostMintQuoteResponse: Mint Quote Response
        """
        mint_quote_response = await super().mint_quote(amount, self.unit)
        decoded_invoice = bolt11.decode(mint_quote_response.request)
        invoice = Invoice(
            amount=amount,
            bolt11=mint_quote_response.request,
            payment_hash=decoded_invoice.payment_hash,
            id=mint_quote_response.quote,
            out=False,
            time_created=int(time.time()),
        )
        await store_lightning_invoice(db=self.db, invoice=invoice)
        return invoice

    def split_wallet_state(self, amount: int) -> List[int]:
        """This function produces an amount split for outputs based on the current state of the wallet.
        Its objective is to fill up the wallet so that it reaches `n_target` coins of each amount.

        Args:
            amount (int): Amount to split

        Returns:
            List[int]: List of amounts to mint
        """
        # read the target count for each amount from settings
        n_target = settings.wallet_target_amount_count
        amounts_we_have = [p.amount for p in self.proofs if p.reserved is not True]
        amounts_we_have.sort()
        # NOTE: Do not assume 2^n here
        all_possible_amounts: list[int] = [2**i for i in range(settings.max_order)]
        amounts_we_want_ll = [
            [a] * max(0, n_target - amounts_we_have.count(a))
            for a in all_possible_amounts
        ]
        # flatten list of lists to list
        amounts_we_want = [item for sublist in amounts_we_want_ll for item in sublist]
        # sort by increasing amount
        amounts_we_want.sort()

        logger.debug(
            f"Amounts we have: {[(a, amounts_we_have.count(a)) for a in set(amounts_we_have)]}"
        )
        amounts: list[int] = []
        while sum(amounts) < amount and amounts_we_want:
            if sum(amounts) + amounts_we_want[0] > amount:
                break
            amounts.append(amounts_we_want.pop(0))

        remaining_amount = amount - sum(amounts)
        if remaining_amount > 0:
            amounts += amount_split(remaining_amount)

        logger.debug(f"Amounts we want: {amounts}")
        if sum(amounts) != amount:
            raise Exception(f"Amounts do not sum to {amount}.")

        return amounts

    async def mint(
        self,
        amount: int,
        id: str,
        split: Optional[List[int]] = None,
    ) -> List[Proof]:
        """Mint tokens of a specific amount after an invoice has been paid.

        Args:
            amount (int): Total amount of tokens to be minted
            id (str): Id for looking up the paid Lightning invoice.
            split (Optional[List[str]], optional): List of desired amount splits to be minted. Total must sum to `amount`.

        Raises:
            Exception: Raises exception if `amounts` does not sum to `amount` or has unsupported value.
            Exception: Raises exception if no proofs have been provided

        Returns:
            List[Proof]: Newly minted proofs.
        """
        # specific split
        if split:
            logger.trace(f"Mint with split: {split}")
            assert sum(split) == amount, "split must sum to amount"
            allowed_amounts = [2**i for i in range(settings.max_order)]
            for a in split:
                if a not in allowed_amounts:
                    raise Exception(
                        f"Can only mint amounts with 2^n up to {2**settings.max_order}."
                    )

        # split based on our wallet state
        amounts = split or self.split_wallet_state(amount)
        # if no split was specified, we use the canonical split
        # amounts = split or amount_split(amount)

        # quirk: we skip bumping the secret counter in the database since we are
        # not sure if the minting will succeed. If it succeeds, we will bump it
        # in the next step.
        secrets, rs, derivation_paths = await self.generate_n_secrets(
            len(amounts), skip_bump=True
        )
        await self._check_used_secrets(secrets)
        outputs, rs = self._construct_outputs(amounts, secrets, rs)

        # will raise exception if mint is unsuccessful
        promises = await super().mint(outputs, id)

        promises_keyset_id = promises[0].id
        await bump_secret_derivation(
            db=self.db, keyset_id=promises_keyset_id, by=len(amounts)
        )
        proofs = await self._construct_proofs(promises, secrets, rs, derivation_paths)

        if id:
            await update_lightning_invoice(
                db=self.db, id=id, paid=True, time_paid=int(time.time())
            )
            # store the mint_id in proofs
            async with self.db.connect() as conn:
                for p in proofs:
                    p.mint_id = id
                    await update_proof(p, mint_id=id, conn=conn)
        return proofs

    async def redeem(
        self,
        proofs: List[Proof],
    ) -> Tuple[List[Proof], List[Proof]]:
        """Redeem proofs by sending them to yourself (by calling a split).)
        Calls `add_witnesses_to_proofs` which parses all proofs and checks whether their
        secrets corresponds to any locks that we have the unlock conditions for. If so,
        it adds the unlock conditions to the proofs.
        Args:
            proofs (List[Proof]): Proofs to be redeemed.
        """
        # verify DLEQ of incoming proofs
        self.verify_proofs_dleq(proofs)
        return await self.split(proofs=proofs, amount=0)

    def swap_send_and_keep_output_amounts(
        self, proofs: List[Proof], amount: int
    ) -> Tuple[List[int], List[int]]:
        """This function generates a suitable amount split for the outputs to keep and the outputs to send. It
        calculates the amount to keep based on the wallet state and the amount to send based on the amount
        provided.

        Args:
            proofs (List[Proof]): Proofs to be split.
            amount (int): Amount to be sent.

        Returns:
            Tuple[List[int], List[int]]: Two lists of amounts, one for keeping and one for sending.
        """
        # create a suitable amount split based on the proofs provided
        total = sum_proofs(proofs)
        keep_amt, send_amt = total - amount, amount
        logger.trace(f"Keep amount: {keep_amt}, send amount: {send_amt}")
        logger.trace(f"Total input: {sum_proofs(proofs)}")
        # generate splits for outputs
        send_output_amounts_without_fee = amount_split(send_amt)
        # add fees to outputs to send because we're nice
        # TODO: fees_for_outputs does not include the fees to pay for themselves!
        fees_for_outputs = amount_split(
            self.get_fees_for_keyset(
                send_output_amounts_without_fee, self.keysets[self.keyset_id]
            )
        )
        send_outputs = send_output_amounts_without_fee + fees_for_outputs
        logger.trace(
            f"Send {sum(send_output_amounts_without_fee)} plus fees: {sum(fees_for_outputs)}"
        )
        # we subtract the fee we add to the output from the amount to keep
        keep_amt -= sum(fees_for_outputs)
        logger.trace(f"Keep amount: {keep_amt}")

        # we subtract the fee for the entire transaction from the amount to keep
        keep_amt -= self.get_fees_for_proofs(proofs)
        logger.trace(f"Keep amount: {keep_amt}")

        # we determine the amounts to keep based on the wallet state
        keep_outputs = self.split_wallet_state(keep_amt)

        return keep_outputs, send_outputs

    async def split(
        self,
        proofs: List[Proof],
        amount: int,
        secret_lock: Optional[Secret] = None,
    ) -> Tuple[List[Proof], List[Proof]]:
        """Calls the swap API to split the proofs into two sets of proofs, one for keeping and one for sending.

        If secret_lock is None, random secrets will be generated for the tokens to keep (keep_outputs)
        and the promises to send (send_outputs). If secret_lock is provided, the wallet will create
        blinded secrets with those to attach a predefined spending condition to the tokens they want to send.

        Args:
            proofs (List[Proof]): Proofs to be split.
            amount (int): Amount to be sent.
            secret_lock (Optional[Secret], optional): Secret to lock the tokens to be sent. Defaults to None.

        Returns:
            Tuple[List[Proof], List[Proof]]: Two lists of proofs, one for keeping and one for sending.
        """
        assert len(proofs) > 0, "no proofs provided."
        assert sum_proofs(proofs) >= amount, "amount too large."
        assert amount >= 0, "amount can't be negative."
        # make sure we're operating on an independent copy of proofs
        proofs = copy.copy(proofs)

        # potentially add witnesses to unlock provided proofs (if they indicate one)
        proofs = await self.add_witnesses_to_proofs(proofs)

        # create a suitable amount lists to keep and send based on the proofs
        # provided and the state of the wallet
        keep_outputs, send_outputs = self.swap_send_and_keep_output_amounts(
            proofs, amount
        )

        amounts = keep_outputs + send_outputs
        # generate secrets for new outputs
        if secret_lock is None:
            secrets, rs, derivation_paths = await self.generate_n_secrets(len(amounts))
        else:
            # NOTE: we use random blinding factors for locks, we won't be able to
            # restore these tokens from a backup
            rs = []
            # generate secrets for receiver
            secret_locks = [secret_lock.serialize() for i in range(len(send_outputs))]
            logger.debug(f"Creating proofs with custom secrets: {secret_locks}")
            # append predefined secrets (to send) to random secrets (to keep)
            # generate secrets to keep
            secrets = [
                await self._generate_secret() for s in range(len(keep_outputs))
            ] + secret_locks
            # TODO: derive derivation paths from secrets
            derivation_paths = ["custom"] * len(secrets)

        assert len(secrets) == len(
            amounts
        ), "number of secrets does not match number of outputs"
        # verify that we didn't accidentally reuse a secret
        await self._check_used_secrets(secrets)

        # construct outputs
        outputs, rs = self._construct_outputs(amounts, secrets, rs)

        # potentially add witnesses to outputs based on what requirement the proofs indicate
        outputs = await self.add_witnesses_to_outputs(proofs, outputs)

        # Call swap API
        promises = await super().split(proofs, outputs)

        # Construct proofs from returned promises (i.e., unblind the signatures)
        new_proofs = await self._construct_proofs(
            promises, secrets, rs, derivation_paths
        )

        await self.invalidate(proofs)

        keep_proofs = new_proofs[: len(keep_outputs)]
        send_proofs = new_proofs[len(keep_outputs) :]
        return keep_proofs, send_proofs

    async def request_melt(
        self, invoice: str, amount: Optional[int] = None
    ) -> PostMeltQuoteResponse:
        """
        Fetches a melt quote from the mint and either uses the amount in the invoice or the amount provided.
        """
        if amount and not self.mint_info.supports_mpp("bolt11", self.unit):
            raise Exception("Mint does not support MPP, cannot specify amount.")
        melt_quote = await self.melt_quote(invoice, self.unit, amount)
        logger.debug(
            f"Mint wants {self.unit.str(melt_quote.fee_reserve)} as fee reserve."
        )
        return melt_quote

    async def melt(
        self, proofs: List[Proof], invoice: str, fee_reserve_sat: int, quote_id: str
    ) -> PostMeltResponse:
        """Pays a lightning invoice and returns the status of the payment.

        Args:
            proofs (List[Proof]): List of proofs to be spent.
            invoice (str): Lightning invoice to be paid.
            fee_reserve_sat (int): Amount of fees to be reserved for the payment.

        """
        # Make sure we're operating on an independent copy of proofs
        proofs = copy.copy(proofs)

        # Generate a number of blank outputs for any overpaid fees. As described in
        # NUT-08, the mint will imprint these outputs with a value depending on the
        # amount of fees we overpaid.
        n_change_outputs = calculate_number_of_blank_outputs(fee_reserve_sat)
        (
            change_secrets,
            change_rs,
            change_derivation_paths,
        ) = await self.generate_n_secrets(n_change_outputs)
        change_outputs, change_rs = self._construct_outputs(
            n_change_outputs * [1], change_secrets, change_rs
        )

        # store the melt_id in proofs db
        async with self.db.connect() as conn:
            for p in proofs:
                p.melt_id = quote_id
                await update_proof(p, melt_id=quote_id, conn=conn)

        # we store the invoice object in the database to later be able to check the invoice state

        decoded_invoice = bolt11.decode(invoice)
        invoice_obj = Invoice(
            amount=-sum_proofs(proofs),
            bolt11=invoice,
            payment_hash=decoded_invoice.payment_hash,
            # preimage=status.preimage,
            paid=False,
            time_paid=int(time.time()),
            id=quote_id,  # store the same ID in the invoice
            out=True,  # outgoing invoice
        )
        # store invoice in db as not paid yet
        await store_lightning_invoice(db=self.db, invoice=invoice_obj)

        status = await super().melt(quote_id, proofs, change_outputs)

        # if payment fails
        if not status.paid:
            # remove the melt_id in proofs
            for p in proofs:
                p.melt_id = None
                await update_proof(p, melt_id=None, db=self.db)
            raise Exception("could not pay invoice.")

        # invoice was paid successfully

        await self.invalidate(proofs)

        # update paid status in db
        logger.trace(f"Settings invoice {quote_id} to paid.")
        await update_lightning_invoice(
            db=self.db,
            id=quote_id,
            paid=True,
            time_paid=int(time.time()),
            preimage=status.payment_preimage,
        )

        # handle change and produce proofs
        if status.change:
            change_proofs = await self._construct_proofs(
                status.change,
                change_secrets[: len(status.change)],
                change_rs[: len(status.change)],
                change_derivation_paths[: len(status.change)],
            )
            logger.debug(f"Received change: {self.unit.str(sum_proofs(change_proofs))}")
        return status

    async def check_proof_state(self, proofs) -> PostCheckStateResponse:
        return await super().check_proof_state(proofs)

    # ---------- TOKEN MECHANICS ----------

    # ---------- DLEQ PROOFS ----------

    def verify_proofs_dleq(self, proofs: List[Proof]):
        """Verifies DLEQ proofs in proofs."""
        for proof in proofs:
            if not proof.dleq:
                logger.trace("No DLEQ proof in proof.")
                return
            logger.trace("Verifying DLEQ proof.")
            assert proof.id
            assert (
                proof.id in self.keysets
            ), f"Keyset {proof.id} not known, can not verify DLEQ."
            if not b_dhke.carol_verify_dleq(
                secret_msg=proof.secret,
                C=PublicKey(bytes.fromhex(proof.C), raw=True),
                r=PrivateKey(bytes.fromhex(proof.dleq.r), raw=True),
                e=PrivateKey(bytes.fromhex(proof.dleq.e), raw=True),
                s=PrivateKey(bytes.fromhex(proof.dleq.s), raw=True),
                A=self.keysets[proof.id].public_keys[proof.amount],
            ):
                raise Exception("DLEQ proof invalid.")
            else:
                logger.trace("DLEQ proof valid.")
        logger.debug("Verified incoming DLEQ proofs.")

    async def _construct_proofs(
        self,
        promises: List[BlindedSignature],
        secrets: List[str],
        rs: List[PrivateKey],
        derivation_paths: List[str],
    ) -> List[Proof]:
        """Constructs proofs from promises, secrets, rs and derivation paths.

        This method is called after the user has received blind signatures from
        the mint. The results are proofs that can be used as ecash.

        Args:
            promises (List[BlindedSignature]): blind signatures from mint
            secrets (List[str]): secrets that were previously used to create blind messages (that turned into promises)
            rs (List[PrivateKey]): blinding factors that were previously used to create blind messages (that turned into promises)
            derivation_paths (List[str]): derivation paths that were used to generate secrets and blinding factors

        Returns:
            List[Proof]: list of proofs that can be used as ecash
        """
        logger.trace("Constructing proofs.")
        proofs: List[Proof] = []
        for promise, secret, r, path in zip(promises, secrets, rs, derivation_paths):
            if promise.id not in self.keysets:
                logger.debug(f"Keyset {promise.id} not found in db. Loading from mint.")
                # we don't have the keyset for this promise, so we load all keysets from the mint
                await self.load_mint_keysets()
                assert promise.id in self.keysets, "Could not load keyset."
            C_ = PublicKey(bytes.fromhex(promise.C_), raw=True)
            C = b_dhke.step3_alice(
                C_, r, self.keysets[promise.id].public_keys[promise.amount]
            )

            if not settings.wallet_use_deprecated_h2c:
                B_, r = b_dhke.step1_alice(secret, r)  # recompute B_ for dleq proofs
            # BEGIN: BACKWARDS COMPATIBILITY < 0.15.1
            else:
                B_, r = b_dhke.step1_alice_deprecated(
                    secret, r
                )  # recompute B_ for dleq proofs
            # END: BACKWARDS COMPATIBILITY < 0.15.1

            proof = Proof(
                id=promise.id,
                amount=promise.amount,
                C=C.serialize().hex(),
                secret=secret,
                derivation_path=path,
            )

            # if the mint returned a dleq proof, we add it to the proof
            if promise.dleq:
                proof.dleq = DLEQWallet(
                    e=promise.dleq.e, s=promise.dleq.s, r=r.serialize()
                )

            proofs.append(proof)

            logger.trace(
                f"Created proof: {proof}, r: {r.serialize()} out of promise {promise}"
            )

        # DLEQ verify
        self.verify_proofs_dleq(proofs)

        logger.trace(f"Constructed {len(proofs)} proofs.")

        # add new proofs to wallet
        self.proofs += copy.copy(proofs)
        # store new proofs in database
        await self._store_proofs(proofs)

        return proofs

    def _construct_outputs(
        self,
        amounts: List[int],
        secrets: List[str],
        rs: List[PrivateKey] = [],
    ) -> Tuple[List[BlindedMessage], List[PrivateKey]]:
        """Takes a list of amounts and secrets and returns outputs.
        Outputs are blinded messages `outputs` and blinding factors `rs`

        Args:
            amounts (List[int]): list of amounts
            secrets (List[str]): list of secrets
            rs (List[PrivateKey], optional): list of blinding factors. If not given, `rs` are generated in step1_alice. Defaults to [].

        Returns:
            List[BlindedMessage]: list of blinded messages that can be sent to the mint
            List[PrivateKey]: list of blinding factors that can be used to construct proofs after receiving blind signatures from the mint

        Raises:
            AssertionError: if len(amounts) != len(secrets)
        """
        assert len(amounts) == len(
            secrets
        ), f"len(amounts)={len(amounts)} not equal to len(secrets)={len(secrets)}"
        outputs: List[BlindedMessage] = []

        rs_ = [None] * len(amounts) if not rs else rs
        rs_return: List[PrivateKey] = []
        for secret, amount, r in zip(secrets, amounts, rs_):
            if not settings.wallet_use_deprecated_h2c:
                B_, r = b_dhke.step1_alice(secret, r or None)
            # BEGIN: BACKWARDS COMPATIBILITY < 0.15.1
            else:
                B_, r = b_dhke.step1_alice_deprecated(secret, r or None)
            # END: BACKWARDS COMPATIBILITY < 0.15.1

            rs_return.append(r)
            output = BlindedMessage(
                amount=amount, B_=B_.serialize().hex(), id=self.keyset_id
            )
            outputs.append(output)
            logger.trace(f"Constructing output: {output}, r: {r.serialize()}")

        return outputs, rs_return

    async def _store_proofs(self, proofs):
        try:
            async with self.db.connect() as conn:
                for proof in proofs:
                    await store_proof(proof, db=self.db, conn=conn)
        except Exception as e:
            logger.error(f"Could not store proofs in database: {e}")
            logger.error(proofs)
            raise e

    async def invalidate(
        self, proofs: List[Proof], check_spendable=False
    ) -> List[Proof]:
        """Invalidates all unspendable tokens supplied in proofs.

        Args:
            proofs (List[Proof]): Which proofs to delete
            check_spendable (bool, optional): Asks the mint to check whether proofs are already spent before deleting them. Defaults to False.

        Returns:
            List[Proof]: List of proofs that are still spendable.
        """
        invalidated_proofs: List[Proof] = []
        if check_spendable:
            proof_states = await self.check_proof_state(proofs)
            for i, state in enumerate(proof_states.states):
                if state.state == SpentState.spent:
                    invalidated_proofs.append(proofs[i])
        else:
            invalidated_proofs = proofs

        if invalidated_proofs:
            logger.trace(
                f"Invalidating {len(invalidated_proofs)} proofs worth"
                f" {self.unit.str(sum_proofs(invalidated_proofs))}."
            )

        async with self.db.connect() as conn:
            for p in invalidated_proofs:
                await invalidate_proof(p, db=self.db, conn=conn)

        invalidate_secrets = [p.secret for p in invalidated_proofs]
        self.proofs = list(
            filter(lambda p: p.secret not in invalidate_secrets, self.proofs)
        )
        return [p for p in proofs if p not in invalidated_proofs]

    # ---------- TRANSACTION HELPERS ----------

    async def get_pay_amount_with_fees(self, invoice: str):
        """
        Decodes the amount from a Lightning invoice and returns the
        total amount (amount+fees) to be paid.
        """
        melt_quote = await self.melt_quote(invoice, self.unit)
        logger.debug(
            f"Mint wants {self.unit.str(melt_quote.fee_reserve)} as fee reserve."
        )
        return melt_quote

    async def select_to_send(
        self,
        proofs: List[Proof],
        amount: int,
        set_reserved: bool = False,
        offline: bool = False,
        tolerance: int = 0,
    ) -> Tuple[List[Proof], int]:
        """
        Selects proofs such that a certain amount can be sent.

        Args:
            proofs (List[Proof]): Proofs to split
            amount (int): Amount to split to
            set_reserved (bool, optional): If set, the proofs are marked as reserved.

        Returns:
            List[Proof]: Proofs to send
            int: Fees for the transaction
        """
        # TODO: load mint from database for offline mode!
        await self.load_mint()

        # select proofs that are not reserved
        proofs = [p for p in proofs if not p.reserved]
        # select proofs that are in the keysets of the mint
        proofs = [p for p in proofs if p.id in self.keysets]

        send_proofs, fees = await self._select_proofs_to_send(proofs, amount, tolerance)
        if not send_proofs and offline:
            raise Exception(
                "Could not select proofs in offline mode. Available amounts:"
                f" {', '.join([Amount(self.unit, p.amount).str() for p in proofs])}"
            )

        if not send_proofs and not offline:
            # we set the proofs as reserved later
            _, send_proofs = await self.split_to_send(
                proofs, amount, set_reserved=False
            )

        if set_reserved:
            await self.set_reserved(send_proofs, reserved=True)
        return send_proofs, fees

    async def split_to_send(
        self,
        proofs: List[Proof],
        amount: int,
        secret_lock: Optional[Secret] = None,
        set_reserved: bool = False,
    ) -> Tuple[List[Proof], List[Proof]]:
        """
        Splits proofs such that a desired amount can be sent.

        Args:
            proofs (List[Proof]): Proofs to split
            amount (int): Amount to split to
            secret_lock (Optional[str], optional): If set, a custom secret is used to lock new outputs. Defaults to None.
            set_reserved (bool, optional): If set, the proofs are marked as reserved. Should be set to False if a payment attempt
            is made with the split that could fail (like a Lightning payment). Should be set to True if the token to be sent is
            displayed to the user to be then sent to someone else. Defaults to False.

        Returns:
            Tuple[List[Proof], List[Proof]]: Tuple of proofs to keep and proofs to send
        """
        # select proofs that are not reserved
        proofs = [p for p in proofs if not p.reserved]

        # select proofs that are in the active keysets of the mint
        proofs = [p for p in proofs if p.id in self.keysets]

        spendable_proofs, fees = await self._select_proofs_to_split(proofs, amount)
        logger.debug(
            f"Amount to send: {self.unit.str(amount)} (+ {self.unit.str(fees)} fees)"
        )
        if secret_lock:
            logger.debug(f"Spending conditions: {secret_lock}")
        keep_proofs, send_proofs = await self.split(
            spendable_proofs, amount, secret_lock
        )
        if set_reserved:
            await self.set_reserved(send_proofs, reserved=True)
        return keep_proofs, send_proofs

    # ---------- BALANCE CHECKS ----------

    @property
    def balance(self):
        return sum_proofs(self.proofs)

    @property
    def available_balance(self):
        return sum_proofs([p for p in self.proofs if not p.reserved])

    @property
    def proof_amounts(self):
        """Returns a sorted list of amounts of all proofs"""
        return [p.amount for p in sorted(self.proofs, key=lambda p: p.amount)]

    def balance_per_keyset(self) -> Dict[str, Dict[str, Union[int, str]]]:
        ret: Dict[str, Dict[str, Union[int, str]]] = {
            key: {
                "balance": sum_proofs(proofs),
                "available": sum_proofs([p for p in proofs if not p.reserved]),
            }
            for key, proofs in self._get_proofs_per_keyset(self.proofs).items()
        }
        for key in ret.keys():
            if key in self.keysets:
                ret[key]["unit"] = self.keysets[key].unit.name
        return ret

    def balance_per_unit(self) -> Dict[Unit, Dict[str, Union[int, str]]]:
        ret: Dict[Unit, Dict[str, Union[int, str]]] = {
            unit: {
                "balance": sum_proofs(proofs),
                "available": sum_proofs([p for p in proofs if not p.reserved]),
            }
            for unit, proofs in self._get_proofs_per_unit(self.proofs).items()
        }
        return ret

    async def balance_per_minturl(
        self, unit: Optional[Unit] = None
    ) -> Dict[str, Dict[str, Union[int, str]]]:
        balances = await self._get_proofs_per_minturl(self.proofs, unit=unit)
        balances_return: Dict[str, Dict[str, Union[int, str]]] = {
            key: {
                "balance": sum_proofs(proofs),
                "available": sum_proofs([p for p in proofs if not p.reserved]),
            }
            for key, proofs in balances.items()
        }
        for key in balances_return.keys():
            if unit:
                balances_return[key]["unit"] = unit.name
        return dict(sorted(balances_return.items(), key=lambda item: item[0]))  # type: ignore

    async def restore_tokens_for_keyset(
        self, keyset_id: str, to: int = 2, batch: int = 25
    ) -> None:
        """
        Restores tokens for a given keyset_id.

        Args:
            keyset_id (str): The keyset_id to restore tokens for.
            to (int, optional): The number of consecutive empty responses to stop restoring. Defaults to 2.
            batch (int, optional): The number of proofs to restore in one batch. Defaults to 25.
        """
        stop_counter = 0
        # we get the current secret counter and restore from there on
        spendable_proofs = []
        counter_before = await bump_secret_derivation(
            db=self.db, keyset_id=keyset_id, by=0
        )
        if counter_before != 0:
            print("Keyset has already been used. Restoring from it's last state.")
        i = counter_before
        n_last_restored_proofs = 0
        while stop_counter < to:
            print(f"Restoring token {i} to {i + batch}...")
            restored_proofs = await self.restore_promises_from_to(i, i + batch - 1)
            if len(restored_proofs) == 0:
                stop_counter += 1
            spendable_proofs = await self.invalidate(
                restored_proofs, check_spendable=True
            )
            if len(spendable_proofs):
                n_last_restored_proofs = len(spendable_proofs)
                print(f"Restored {sum_proofs(restored_proofs)} sat")
            i += batch

        # restore the secret counter to its previous value for the last round
        revert_counter_by = batch * to + n_last_restored_proofs
        logger.debug(f"Reverting secret counter by {revert_counter_by}")
        before = await bump_secret_derivation(
            db=self.db,
            keyset_id=keyset_id,
            by=-revert_counter_by,
        )
        logger.debug(
            f"Secret counter reverted from {before} to {before - revert_counter_by}"
        )
        if n_last_restored_proofs == 0:
            print("No tokens restored for keyset.")
            return

    async def restore_wallet_from_mnemonic(
        self, mnemonic: Optional[str], to: int = 2, batch: int = 25
    ) -> None:
        """
        Restores the wallet from a mnemonic.

        Args:
            mnemonic (Optional[str]): The mnemonic to restore the wallet from. If None, the mnemonic is loaded from the db.
            to (int, optional): The number of consecutive empty responses to stop restoring. Defaults to 2.
            batch (int, optional): The number of proofs to restore in one batch. Defaults to 25.
        """
        await self._init_private_key(mnemonic)
        await self.load_mint()
        print("Restoring tokens...")
        for keyset_id in self.keysets.keys():
            await self.restore_tokens_for_keyset(keyset_id, to, batch)

    async def restore_promises_from_to(
        self, from_counter: int, to_counter: int
    ) -> List[Proof]:
        """Restores promises from a given range of counters. This is for restoring a wallet from a mnemonic.

        Args:
            from_counter (int): Counter for the secret derivation to start from
            to_counter (int): Counter for the secret derivation to end at

        Returns:
            List[Proof]: List of restored proofs
        """
        # we regenerate the secrets and rs for the given range
        secrets, rs, derivation_paths = await self.generate_secrets_from_to(
            from_counter, to_counter
        )
        # we don't know the amount but luckily the mint will tell us so we use a dummy amount here
        amounts_dummy = [1] * len(secrets)
        # we generate outputs from deterministic secrets and rs
        regenerated_outputs, _ = self._construct_outputs(amounts_dummy, secrets, rs)
        # we ask the mint to reissue the promises
        proofs = await self.restore_promises(
            outputs=regenerated_outputs,
            secrets=secrets,
            rs=rs,
            derivation_paths=derivation_paths,
        )

        await set_secret_derivation(
            db=self.db, keyset_id=self.keyset_id, counter=to_counter + 1
        )
        return proofs

    async def restore_promises(
        self,
        outputs: List[BlindedMessage],
        secrets: List[str],
        rs: List[PrivateKey],
        derivation_paths: List[str],
    ) -> List[Proof]:
        """Restores proofs from a list of outputs, secrets, rs and derivation paths.

        Args:
            outputs (List[BlindedMessage]): Outputs for which we request promises
            secrets (List[str]): Secrets generated for the outputs
            rs (List[PrivateKey]): Random blinding factors generated for the outputs
            derivation_paths (List[str]): Derivation paths used for the secrets necessary to unblind the promises

        Returns:
            List[Proof]: List of restored proofs
        """
        # restored_outputs is there so we can match the promises to the secrets and rs
        restored_outputs, restored_promises = await super().restore_promises(outputs)
        # now we need to filter out the secrets and rs that had a match
        matching_indices = [
            idx
            for idx, val in enumerate(outputs)
            if val.B_ in [o.B_ for o in restored_outputs]
        ]
        secrets = [secrets[i] for i in matching_indices]
        rs = [rs[i] for i in matching_indices]
        # now we can construct the proofs with the secrets and rs
        proofs = await self._construct_proofs(
            restored_promises, secrets, rs, derivation_paths
        )
        logger.debug(f"Restored {len(restored_promises)} promises")
        return proofs
